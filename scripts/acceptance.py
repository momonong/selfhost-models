"""Real service acceptance. Faults affect only this project's worker/API containers."""
import argparse
import asyncio
import contextlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selfhost_models.cli import compose_command

if __package__:
    from .deployment_target import validate_target
else:
    from deployment_target import validate_target

PROJECT = Path(__file__).resolve().parents[1]


async def main(args):
    state = args.state.resolve()
    if args.faults:
        validate_target(state, args.url)
    key = (state / "api-key").read_text().strip()
    rows = []

    def record(name, **values):
        row = {"check": name, "at": datetime.now(timezone.utc).isoformat(), **values}
        rows.append(row)
        print(json.dumps(row), flush=True)

    def compose(*parts):
        return subprocess.check_output([*compose_command(state), *parts], text=True).strip()

    async with httpx.AsyncClient(base_url=args.url, headers={"Authorization": "Bearer " + key},
                                timeout=65, trust_env=False) as client:
        async def health():
            return (await client.get("/health/ready")).json()

        async def wait_for(predicate, seconds=300):
            until = time.monotonic() + seconds
            last = None
            while time.monotonic() < until:
                try:
                    last = await health()
                    if predicate(last):
                        return last
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            raise AssertionError(f"health condition not reached: {last}")

        async def post(**overrides):
            return await client.post("/v1/chat/completions", json={**payload, **overrides})

        await wait_for(lambda h: h["ready"], 600)
        models = await client.get("/v1/models")
        assert models.status_code == 200
        model = models.json()["data"][0]
        payload = {"model": model["id"], "messages": [{"role": "user", "content": "Reply with the word READY."}],
                   "max_tokens": 32, "temperature": 0}
        record("models", model=model)
        r = await post()
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["finish_reason"] is not None
        record("non_stream", status=r.status_code, request_id=r.headers["x-request-id"], usage=r.json().get("usage"))
        pieces, done = 0, False
        async with client.stream("POST", "/v1/chat/completions", json={**payload, "stream": True,
                                 "stream_options": {"include_usage": True}}) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                    else:
                        assert "error" not in json.loads(data)
                        pieces += 1
        assert done and pieces > 0
        record("stream", chunks=pieces, done=done)
        for name, data, expected in [
            ("unsupported_parameter", {**payload, "logprobs": True}, 400),
            ("bad_tokens", {**payload, "max_tokens": 0}, 400),
            ("unknown_model", {**payload, "model": "unknown/model"}, 404),
            ("context_limit", {**payload, "messages": [{"role": "user", "content": "test " * 6000}]}, 400),
        ]:
            r = await client.post("/v1/chat/completions", json=data)
            assert r.status_code == expected, (name, r.status_code, r.text)
            record(name, status=r.status_code)
        r = await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        record("authentication", status=r.status_code)
        r = await client.post("/v1/chat/completions", content=b"x" * (2097152 + 1),
                              headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        record("body_limit", status=r.status_code)
        if args.faults:
            worker = compose("ps", "-q", "worker")
            assert worker and "\n" not in worker
            info = json.loads(subprocess.check_output(["docker", "inspect", worker], text=True))[0]
            assert info["Config"]["Labels"]["com.docker.compose.project"] == "selfhost-models"
            assert info["Config"]["Labels"]["com.docker.compose.service"] == "worker"

            def pause():
                subprocess.run(["docker", "pause", worker], check=True, stdout=subprocess.DEVNULL)

            def resume():
                subprocess.run(["docker", "unpause", worker], check=True, stdout=subprocess.DEVNULL)

            try:
                await wait_for(lambda h: h["ready"] and h["inflight"] == 0)
                pause()
                # Fast burst before the health probe marks a paused worker unavailable.
                pending = [asyncio.create_task(client.post("/v1/chat/completions", json=payload,
                           headers={"X-Request-Timeout-Ms": "1000"})) for _ in range(8)]
                responses = await asyncio.gather(*pending)
                statuses = [r.status_code for r in responses]
                assert 429 in statuses and 504 in statuses, statuses
                h = await health()
                assert h["inflight"] > 0 and h["detached"] > 0, h
                record("overload_and_deadline", statuses=statuses, retained=h)
            finally:
                resume()
            await wait_for(lambda h: h["ready"] and h["inflight"] == 0)
            record("drain_completed", health=await health())

            try:
                pause()
                pending = asyncio.create_task(post())
                await wait_for(lambda h: h["inflight"] > 0, 2)
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                h = await wait_for(lambda h: h["detached"] > 0, 2)
                old_epoch = h["worker_epoch"]
                record("client_cancel_retains_lease", health=h)
                # Restart API while the still-live worker cannot finish.
                await asyncio.to_thread(compose, "restart", "api")
            finally:
                resume()
            h = await wait_for(lambda h: not h["ready"] and h["uncertain"] > 0)
            record("api_restart_quarantines", health=h)
            await asyncio.to_thread(compose, "stop", "worker")
            r = await post()
            assert r.status_code == 503
            record("worker_unavailable", status=r.status_code)
            await asyncio.to_thread(compose, "start", "worker")
            h = await wait_for(lambda h: h["ready"] and h["inflight"] == 0, 600)
            assert h["worker_epoch"] != old_epoch
            r = await post()
            assert r.status_code == 200
            record("worker_restart_recovery", status=r.status_code, health=h)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"environment": "real-service", "url": args.url,
            "faults": args.faults, "results": rows}, indent=2), encoding="utf-8")
        record("acceptance_passed", evidence=str(args.output))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:18080")
    p.add_argument("--state", type=Path, default=PROJECT / ".state")
    p.add_argument("--output", type=Path, default=PROJECT / "evidence/acceptance.json")
    p.add_argument("--faults", action="store_true", help="pause/restart only selfhost-models worker/API")
    asyncio.run(main(p.parse_args()))
