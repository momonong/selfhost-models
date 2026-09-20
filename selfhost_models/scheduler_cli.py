"""Local management paths stop here; public jobs contain only typed refs."""
import argparse
import asyncio
import base64
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .scheduler_schema import Deployment, SchedulerConfig, SchedulerError
from .scheduler_store import Store, durable_write, require_native_state


def add_parser(sub):
    p = sub.add_parser("scheduler", help="durable single-host jobs and lifecycle (opt-in)")
    commands = p.add_subparsers(dest="scheduler_command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--config", type=Path)
    asset = commands.add_parser("asset-register")
    asset.add_argument("--path", type=Path, required=True)
    asset.add_argument("--manifest", type=Path)
    verify = commands.add_parser("asset-verify")
    verify.add_argument("ref")
    resume = commands.add_parser("resume-deployment")
    resume.add_argument("ref")
    dep = commands.add_parser("register")
    dep.add_argument("--file", type=Path, required=True)
    for name in ("controller", "collect", "recover-storage", "unload", "status"):
        commands.add_parser(name)
    events = commands.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)
    api = commands.add_parser("api")
    api.add_argument("--port", type=int, default=18081)
    window = commands.add_parser("budget-window", help="operator-only temporary limits; never reset lifetime counters")
    window.add_argument("action", choices=("open", "close", "status"))
    window.add_argument("--name")
    window.add_argument("--loads", type=int)
    window.add_argument("--generations", type=int)
    for name in ("catalog", "submit", "get", "cancel", "result", "upload"):
        cli = commands.add_parser(name)
        cli.add_argument("--url", default="http://127.0.0.1:18081")
        cli.add_argument("--api-key-file", type=Path, required=True, help="read only; client never opens a scheduler database")
        if name in ("get", "cancel", "result"):
            cli.add_argument("job")
        if name in ("submit", "upload"):
            cli.add_argument("--file", type=Path, required=True)
        if name == "submit":
            cli.add_argument("--idempotency-key", required=True)
            cli.add_argument("--urgent", action="store_true")


def main(args):
    command = args.scheduler_command
    if command in ("catalog", "submit", "get", "cancel", "result", "upload"):
        return client_main(args)
    if args.state.resolve() == (Path(__file__).resolve().parents[1] / ".state").resolve():
        raise SchedulerError("scheduler_requires_separate_state")
    require_native_state(args.state)
    config = SchedulerConfig.model_validate(json.loads(args.config.read_bytes())) if getattr(args, "config", None) else None
    store = Store(args.state, config)
    key_path = store.root / "api-key"
    if command == "init":
        if not key_path.exists():
            durable_write(key_path, secrets.token_urlsafe(48).encode())
            key_path.chmod(0o600)
        result = {"initialized": True, "configuration": store.config.model_dump()}
    elif command == "asset-register":
        expected = json.loads(args.manifest.read_bytes()) if args.manifest else None
        result = {"asset_ref": store.asset_register(args.path, expected)}
    elif command == "asset-verify":
        store.asset_verify(args.ref)
        result = {"verified": True, "asset_ref": args.ref}
    elif command == "register":
        result = {"deployment_id": store.register(Deployment.model_validate(json.loads(args.file.read_bytes())))}
    elif command == "collect":
        result = store.collect()
    elif command == "status":
        result = {**store.state(), "budget": store.budget_state(), "budget_windows": store.budget_windows()}
    elif command == "budget-window":
        if args.action == "open":
            store.open_budget_window(args.name or "", args.loads, args.generations)
        elif args.action == "close":
            if not args.name:
                raise SchedulerError("budget_window_name_required")
            store.close_budget_window(args.name)
        result = {"budget": store.budget_state(), "windows": store.budget_windows()}
    elif command == "resume-deployment":
        from filelock import FileLock
        with FileLock(str(store.root / "controller.lock"), timeout=0):
            store.resume_deployment(args.ref)
        result = {"resumed": args.ref}
    elif command == "events":
        result = store.events(args.after, args.limit)
    elif command in ("controller", "recover-storage", "unload"):
        from .scheduler_controller import Controller
        from .scheduler_runtime import DockerProvider
        controller = Controller(store, DockerProvider(store))
        if command == "controller":
            asyncio.run(controller.run())
        elif command == "unload":
            async def unload():
                await controller.start()
                try:
                    state = store.state()
                    if state["phase"] != "unloaded":
                        await controller.provider.unload(state)
                        if not await controller.provider.exited(state):
                            raise SchedulerError("engine_exit_unconfirmed", 503)
                        store.engine_exited(controller.epoch)
                        await controller.cleanup_exited()
                    await controller.provider.release_ownership()
                finally:
                    await controller.close()
            asyncio.run(unload())
        else:
            # Storage recovery also uses the exclusive ownership lock and fencing.
            with controller.lock:
                epoch = store.acquire_controller()
                store.recover_receipts(epoch)
        return 0
    elif command == "api":
        import uvicorn
        from .api import Gateway
        uvicorn.run(Gateway(key=key_path.read_text().strip(), scheduler=store), host="127.0.0.1", port=args.port, workers=1, access_log=False)
        return 0
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def client_main(args):
    command = args.scheduler_command
    url = urlsplit(args.url)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.username or url.password or url.path not in ("", "/"):
        raise SchedulerError("loopback_url_required")
    headers = {"Authorization": "Bearer " + args.api_key_file.read_text().strip()}
    with httpx.Client(base_url=args.url, headers=headers, trust_env=False, timeout=15) as client:
        if command == "catalog":
            response = client.get("/v1/catalog")
        elif command == "submit":
            payload = json.loads(args.file.read_bytes())
            if args.urgent:
                payload["urgent"] = True
            response = client.post("/v1/jobs", json=payload, headers={"Idempotency-Key": args.idempotency_key})
        elif command == "upload":
            with args.file.open("rb") as f:
                data = f.read(1048577)
            if len(data) > 1048576:
                raise SchedulerError("input_too_large", 413)
            response = client.post("/v1/artifacts", json={"media_type": "audio/wav", "data_base64": base64.b64encode(data).decode()})
        elif command == "cancel":
            response = client.post(f"/v1/jobs/{args.job}/cancel", json={})
        else:
            response = client.get(f"/v1/jobs/{args.job}" + ("/result" if command == "result" else ""))
        result = response.json()
        if response.is_error:
            raise SchedulerError(result.get("error", {}).get("code", "api_request_failed"), response.status_code)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
