"""Small authenticated client for the documented Chat Completions subset."""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def stream_text(lines):
    done = False
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        chunk = json.loads(data)
        if "error" in chunk:
            raise ValueError("stream failed: " + str(chunk["error"].get("code", "backend_error")))
        for choice in chunk.get("choices", []):
            text = choice.get("delta", {}).get("content")
            if text:
                yield text
    if not done:
        raise ValueError("stream ended without [DONE]; partial output is not success")


def main():
    # Keep redirected output UTF-8 on Windows as well as Linux.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="?", help="Omit to read stdin")
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--wait", type=float, default=300, help="Readiness wait in seconds")
    args = parser.parse_args()
    try:
        prompt = args.prompt if args.prompt is not None else sys.stdin.read()
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        key = (args.state / "api-key").read_text().strip()
        with httpx.Client(base_url=args.url, trust_env=False, timeout=180,
                          headers={"Authorization": "Bearer " + key}) as client:
            deadline = time.monotonic() + args.wait
            while True:
                try:
                    ready = client.get("/health/ready", timeout=5)
                    if ready.status_code == 200 and ready.json().get("ready") is True:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    raise ValueError("service not ready; check modelctl status and worker logs")
                time.sleep(1)
            response = client.get("/v1/models")
            response.raise_for_status()
            model = response.json()["data"][0]["id"]
            payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": args.max_tokens, "temperature": 0, "stream": args.stream}
            if args.stream:
                with client.stream("POST", "/v1/chat/completions", json=payload) as response:
                    response.raise_for_status()
                    for text in stream_text(response.iter_lines()):
                        print(text, end="", flush=True)
                print()
            else:
                response = client.post("/v1/chat/completions", json=payload)
                response.raise_for_status()
                print(response.json()["choices"][0]["message"]["content"])
        return 0
    except (OSError, ValueError, KeyError, IndexError, httpx.HTTPError) as exc:
        print(f"chat: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
