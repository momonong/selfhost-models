"""Real API/GPU acceptance using generated, non-business MP4 fixtures."""
import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selfhost_models.video import PREFIX, MAX_BYTES

if __package__:
    from .deployment_target import validate_target
    from .video_decode_probe import make_video
else:
    from deployment_target import validate_target
    from video_decode_probe import make_video

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    _, _, containers = validate_target(args.state, args.url)
    rows = []
    def record(name, **values):
        rows.append({"check": name, "at": datetime.now(timezone.utc).isoformat(), **values})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"scope": "synthetic API/GPU; no table-tennis quality assessment", "results": rows}, indent=2), encoding="utf-8")
        print(name, flush=True)
    key = (args.state / "api-key").read_text().strip()
    async with httpx.AsyncClient(base_url=args.url, headers={"Authorization": "Bearer " + key}, timeout=40, trust_env=False) as client:
        async def wait_idle(seconds=600):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                h = (await client.get("/health/ready")).json()
                if h["ready"] and not h["inflight"]:
                    return h
                await asyncio.sleep(.2)
            raise AssertionError(f"not idle: {h}")
        await wait_idle()
        model = (await client.get("/v1/models")).json()["data"][0]
        assert model["backend"] == "vllm" and model["capabilities"]["videos"]
        record("models", model=model)
        args.fixtures.mkdir(parents=True, exist_ok=True)
        def request(raw):
            return {"model": model["id"], "messages": [{"role": "user", "content": [
                {"type": "text", "text": "Briefly list background colors in chronological order with approximate time ranges in seconds."},
                {"type": "video_url", "video_url": {"url": PREFIX + base64.b64encode(raw).decode()}}]}],
                "max_tokens": 128, "temperature": 0}
        for seconds in (2, 10, 30, 60):
            fixture = args.fixtures / f"synthetic-{seconds}s.mp4"
            if not fixture.exists():
                make_video(fixture, seconds)
            raw = fixture.read_bytes()
            payload = request(raw)
            start = time.perf_counter()
            response = await client.post("/v1/chat/completions", json=payload)
            assert response.status_code == 200, response.text
            body = response.json()
            meta = body["video"]
            assert meta["sampled_frames"] == seconds * 2 and meta["duration_seconds"] == seconds
            assert meta["timestamps_seconds"][0] == 0
            assert abs(meta["timestamps_seconds"][-1] - (seconds - 1 / 30)) < .0001
            assert meta["audio_processed"] is False
            record("mp4_json", seconds=seconds, sha256=hashlib.sha256(raw).hexdigest(), elapsed_seconds=time.perf_counter()-start,
                   request_id=response.headers["x-request-id"], usage=body.get("usage"), video=meta,
                   finish_reason=body["choices"][0]["finish_reason"], synthetic_response=body["choices"][0]["message"]["content"])
        chunks, done, meta, usage, text = 0, False, None, None, ""
        start = time.perf_counter()
        async with client.stream("POST", "/v1/chat/completions", json={**payload, "stream": True, "stream_options": {"include_usage": True}}) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    continue
                item = json.loads(data)
                assert "error" not in item
                chunks += 1
                meta = item.get("video") or meta
                usage = item.get("usage") or usage
                text += "".join(c.get("delta", {}).get("content") or "" for c in item.get("choices", []))
        assert done and meta["sampled_frames"] == 120 and usage["prompt_tokens"] > 3840
        record("mp4_sse", done=done, chunks=chunks, video=meta, usage=usage, synthetic_response=text, elapsed_seconds=time.perf_counter()-start)
        for name, bad, expected in [("malformed_mp4", request(b"not mp4"), 400),
                                    ("decoded_size_limit", request(bytes(MAX_BYTES + 1)), 400)]:
            response = await client.post("/v1/chat/completions", json=bad)
            assert response.status_code == expected, response.text
            record(name, status=response.status_code, error=response.json()["error"]["code"])
        long = args.fixtures / "over-60s.mp4"
        make_video(long, 61, 64, 64, 2)
        response = await client.post("/v1/chat/completions", json=request(long.read_bytes()))
        assert response.status_code == 400
        record("duration_limit", status=response.status_code, error=response.json()["error"]["code"])
        # Enough time to receive the small file, but much shorter than full decode.
        large = args.fixtures / "synthetic-60s-720p60.mp4"
        if not large.exists():
            make_video(large, 60, 1280, 720, 60)
        response = await client.post("/v1/chat/completions", json=request(large.read_bytes()), headers={"X-Request-Timeout-Ms": "100"})
        assert response.status_code == 504, response.text
        retained = (await client.get("/health/ready")).json()
        assert retained["inflight"] > 0 and retained["detached"] > 0
        drained = await wait_idle()
        record("decoder_deadline", status=504, retained=retained, drained=drained)
        # Saturate admission with real decoder work, without pausing any service.
        large_payload = request(large.read_bytes())
        burst = await asyncio.gather(*(client.post("/v1/chat/completions", json=large_payload,
            headers={"X-Request-Timeout-Ms": "500"}) for _ in range(4)))
        statuses = [r.status_code for r in burst]
        assert statuses.count(504) == model["max_inflight"] and 429 in statuses, statuses
        retained = (await client.get("/health/ready")).json()
        assert retained["inflight"] > 0
        record("video_overload", statuses=statuses, retained=retained, drained=await wait_idle())
        pending = asyncio.create_task(client.post("/v1/chat/completions", json=large_payload))
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if (await client.get("/health/ready")).json()["inflight"]:
                break
            await asyncio.sleep(.01)
        else:
            raise AssertionError("video not admitted")
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending
        until = time.monotonic() + 3
        while time.monotonic() < until:
            retained = (await client.get("/health/ready")).json()
            if retained["detached"]:
                break
            await asyncio.sleep(.01)
        assert retained["inflight"] and retained["detached"]
        record("video_disconnect", retained=retained, drained=await wait_idle())
        check = subprocess.check_output(["docker", "exec", containers["api"]["Id"], "python", "-c",
            "import json; from pathlib import Path; print(json.dumps([p.name for p in Path('/tmp').glob('selfhost-video-*')]))"], text=True)
        assert json.loads(check) == []
        record("temporary_cleanup", remaining=[], health=await wait_idle())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--fixtures", type=Path, default=ROOT / ".state/video-fixtures")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
