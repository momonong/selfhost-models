"""Bounded faults after real SSE headers: deadline and disconnected consumer."""
import argparse
import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

if __package__:
    from .deployment_target import validate_target
else:
    from deployment_target import validate_target

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    state = args.state.resolve()
    compose, settings, containers = validate_target(state, args.url)
    worker = containers["worker"]["Id"]
    key = (state / "api-key").read_text().strip()
    records = []
    async with httpx.AsyncClient(base_url=args.url, headers={"Authorization": "Bearer " + key},
                                trust_env=False, timeout=35) as client:
        async def wait_health(predicate):
            until = time.monotonic() + 120
            while time.monotonic() < until:
                h = (await client.get("/health/ready")).json()
                if predicate(h):
                    return h
                await asyncio.sleep(0.1)
            raise AssertionError(f"health did not reach expected state: {h}")

        model = (await client.get("/v1/models")).json()["data"][0]["id"]
        payload = {"model": model, "messages": [{"role": "user", "content":
            "Write a long numbered list of 100 different common objects, one per line. Continue until item 100."}],
            "max_tokens": 256, "stream": True, "temperature": 0}
        for mode in ("deadline", "disconnect"):
            await wait_health(lambda h: h["ready"] and h["inflight"] == 0)
            paused, saw_error, saw_done = False, False, False
            try:
                async with client.stream("POST", "/v1/chat/completions", json=payload,
                                         headers={"X-Request-Timeout-Ms": "3000"}) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        chunk = None if data == "[DONE]" else json.loads(data)
                        generated_text = chunk is not None and any(
                            c.get("delta", {}).get("content") for c in chunk.get("choices", []))
                        # A role/header-only event does not prove GPU decode began.
                        if not paused and generated_text:
                            subprocess.run(["docker", "pause", worker], check=True, stdout=subprocess.DEVNULL)
                            paused = True
                            if mode == "disconnect":
                                break
                        if data == "[DONE]":
                            saw_done = True
                        elif "error" in json.loads(data):
                            assert json.loads(data)["error"]["code"] == "deadline_exceeded"
                            saw_error = True
                assert paused, "no generated content received before stream ended"
                h = await wait_health(lambda h: h["detached"] > 0 and h["inflight"] > 0)
                if mode == "deadline":
                    assert saw_error and not saw_done
                record = {"mode": mode, "at": datetime.now(timezone.utc).isoformat(),
                          "headers_status": 200, "generated_content_before_fault": True,
                          "url": args.url, "backend": settings.get("BACKEND", "vllm"),
                          "worker_id": worker, "error_frame": saw_error,
                          "done_frame": saw_done, "retained": h}
                records.append(record)
                print(json.dumps(record), flush=True)
            finally:
                if paused:
                    subprocess.run(["docker", "unpause", worker], check=True, stdout=subprocess.DEVNULL)
            h = await wait_health(lambda h: h["ready"] and h["inflight"] == 0)
            records[-1]["drained"] = h
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print("stream lifecycle acceptance passed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/stream-lifecycle.json")
    asyncio.run(main(parser.parse_args()))
