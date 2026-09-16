import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

from .assets import Assets, default_root
from .capabilities import capabilities

PROJECT = Path(__file__).resolve().parents[1]


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def doctor():
    checks = {}
    for name, cmd in {
        "gpu": ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv"],
        "compute": ["nvidia-smi", "--query-compute-apps=pid,process_name,used_gpu_memory", "--format=csv"],
        "docker": ["docker", "info", "--format", "{{.OSType}} {{.OperatingSystem}}"],
        "compose": ["docker", "compose", "version"],
        "containers": ["docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Ports}}"],
    }.items():
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            checks[name] = {"ok": result.returncode == 0, "output": (result.stdout + result.stderr).strip()}
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks[name] = {"ok": False, "output": type(exc).__name__}
    checks["model_root"] = str(default_root())
    print(json.dumps(checks, indent=2, ensure_ascii=False))
    return 0 if all(v["ok"] for v in checks.values() if isinstance(v, dict)) else 1


def compose_command(state):
    env_file = state / "compose.env"
    if not env_file.exists():
        raise ValueError("no deployment configured; run serve first")
    settings = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if line and not line.startswith("#"))
    backend = settings.get("BACKEND", "vllm").strip("'\"")
    if backend not in ("vllm", "transformers"):
        raise ValueError("unsupported configured backend")
    command = ["docker", "compose", "--project-directory", str(PROJECT), "--env-file", str(env_file),
               "-f", str(PROJECT / "compose.yaml")]
    if backend == "transformers":
        command += ["-f", str(PROJECT / "compose.transformers.yaml")]
    return command


def compose(state, args):
    return run([*compose_command(state), *args])


def serve(assets, args):
    record = assets.inspect(args.repo)
    model_type = json.loads((Path(record["path"]) / "config.json").read_text())["model_type"]
    profile = model_type if model_type in ("qwen3_5", "gemma4") else "text"
    caps = capabilities(args.backend, profile)
    if args.max_inflight is None:
        args.max_inflight = 1 if args.backend == "transformers" else 2
    if args.max_inflight > caps["max_inflight_limit"]:
        raise ValueError("capacity exceeds backend limit")
    if args.backend == "transformers" and not 128 <= args.context <= 2048:
        raise ValueError("Transformers validated context range is 128..2048")
    if not 1 <= args.port <= 65535 or not 0.1 <= args.gpu_memory <= 0.9:
        raise ValueError("invalid port / GPU memory fraction")
    with socket.socket() as s:
        s.bind(("127.0.0.1", args.port))  # Do not take over an occupied port.
    # A running deployment may belong to another task. No implicit recreation.
    active = subprocess.check_output(["docker", "ps", "-q", "--filter",
                                     "label=com.docker.compose.project=selfhost-models"], text=True).strip()
    if active:
        raise ValueError("selfhost-models containers already running; use status/restart/stop explicitly")
    assets.state.mkdir(parents=True, exist_ok=True)
    keyfile = assets.state / "api-key"
    if not keyfile.exists():
        with keyfile.open("x", encoding="utf-8") as f:
            f.write(secrets.token_urlsafe(32))
        if os.name != "nt":
            keyfile.chmod(0o600)
    runtime_state = assets.state / "runtime"
    runtime_state.mkdir(exist_ok=True)
    if os.name != "nt":
        runtime_state.chmod(0o700)
    values = {"MODEL_ID": record["repo_id"], "MODEL_REVISION": record["revision"],
              "MODEL_PROFILE": profile, "BACKEND": args.backend,
              "API_UID": str(os.getuid() if os.name != "nt" else 10001),
              "API_GID": str(os.getgid() if os.name != "nt" else 10001),
              "API_STATE_PATH": runtime_state.resolve().as_posix(),
              "MODEL_PATH": Path(record["path"]).as_posix(), "API_KEY_PATH": keyfile.resolve().as_posix(),
              "API_PORT": str(args.port), "GPU_MEMORY_UTILIZATION": str(args.gpu_memory),
              "MAX_INFLIGHT": str(args.max_inflight), "MAX_MODEL_LEN": str(args.context),
              "DEADLINE_SECONDS": str(args.deadline), "DRAIN_SECONDS": str(args.drain)}
    if not 1 <= args.max_inflight <= 16 or not 0 < args.deadline <= args.drain <= 600:
        raise ValueError("invalid capacity or deadlines")
    for value in values.values():
        if any(c in value for c in "\n\r'$"):
            raise ValueError("unsupported character in Compose setting")
    (assets.state / "compose.env").write_text("".join(f"{k}='{v}'\n" for k, v in values.items()), encoding="utf-8")
    compose(assets.state, ["up", "-d", "--build"])
    print(f"API http://127.0.0.1:{args.port}; key file: {keyfile}; poll /health/ready")


def main():
    p = argparse.ArgumentParser(description="Local model assets and Docker Compose lifecycle")
    p.add_argument("--state", type=Path, default=PROJECT / ".state")
    p.add_argument("--model-root", type=Path, default=default_root())
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("list")
    for command in ("inspect", "fetch", "register", "serve"):
        s = sub.add_parser(command)
        s.add_argument("repo")
        if command == "fetch":
            s.add_argument("--revision", default="main")
        elif command == "register":
            s.add_argument("--path", type=Path, required=True)
            s.add_argument("--revision")
        elif command == "serve":
            s.add_argument("--backend", choices=("vllm", "transformers"), default="vllm")
            s.add_argument("--port", type=int, default=18080)
            s.add_argument("--gpu-memory", type=float, default=0.60)
            s.add_argument("--max-inflight", type=int)
            s.add_argument("--context", type=int, default=2048)
            s.add_argument("--deadline", type=float, default=30)
            s.add_argument("--drain", type=float, default=120)
    for command in ("status", "stop", "restart"):
        sub.add_parser(command)
    args = p.parse_args()
    assets = Assets(args.state.resolve(), args.model_root)
    try:
        if args.command == "doctor":
            return doctor()
        if args.command == "serve":
            serve(assets, args)
        elif args.command in ("status", "stop", "restart"):
            cmd = {"status": ["ps"], "stop": ["stop"], "restart": ["restart", "worker", "api"]}[args.command]
            compose(assets.state, cmd)
        else:
            result = getattr(assets, args.command)(**{k: v for k, v in vars(args).items()
                      if k not in ("command", "state", "model_root")})
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"modelctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
