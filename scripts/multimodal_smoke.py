"""Synthetic vision and client-side tool loop; never calls an external service."""
import argparse
import base64
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

PROJECT = Path(__file__).resolve().parents[1]


def main(args):
    key = (args.state / "api-key").read_text().strip()
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer " + key}, timeout=60, trust_env=False) as client:
        model = client.get("/v1/models").json()["data"][0]

        def chat(messages, **kwargs):
            r = client.post("/v1/chat/completions", json={"model": model["id"], "messages": messages,
                           "temperature": 0, "max_tokens": 256, **kwargs})
            assert r.status_code == 200, (r.status_code, r.text)
            return r.json(), r.headers["x-request-id"]

        png = io.BytesIO()
        Image.new("RGB", (224, 224), (255, 0, 0)).save(png, format="PNG")
        uri = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
        result, image_rid = chat([{"role": "user", "content": [
            {"type": "text", "text": "What single color fills this image? Answer with one English color word."},
            {"type": "image_url", "image_url": {"url": uri}}]}])
        color = result["choices"][0]["message"]["content"]
        assert "red" in color.lower(), color
        tools = [{"type": "function", "function": {"name": "lookup_synthetic_code",
            "description": "Look up the synthetic test code. The code is only available from this tool.",
            "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                           "required": ["key"], "additionalProperties": False}}}]
        messages = [{"role": "user", "content": "Use lookup_synthetic_code with key test-42. Then tell me the returned code. Do not invent the code."}]
        result, tool_rid = chat(messages, tools=tools, tool_choice="auto")
        message = result["choices"][0]["message"]
        calls = message.get("tool_calls")
        assert calls and len(calls) == 1, message
        call = calls[0]
        assert call["function"]["name"] == "lookup_synthetic_code"
        assert json.loads(call["function"]["arguments"]) == {"key": "test-42"}
        # Explicit whitelist: the whole tool implementation, no eval/shell/network.
        tool_result = {"code": "731"}
        assistant = {"role": "assistant", "tool_calls": calls}
        if message.get("content"):
            assistant["content"] = message["content"]
        messages.extend([assistant, {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(tool_result)}])
        result, return_rid = chat(messages, tools=tools, tool_choice="none")
        answer = result["choices"][0]["message"]["content"]
        assert "731" in answer, answer
        evidence = {"at": datetime.now(timezone.utc).isoformat(), "model": model,
                    "synthetic_image": {"size": [224, 224], "expected": "red", "actual": color, "request_id": image_rid},
                    "tool_call": {"name": call["function"]["name"], "arguments_valid": True, "request_id": tool_rid},
                    "tool_result_roundtrip": {"expected": "731", "actual": answer, "request_id": return_rid},
                    "claim": "synthetic smoke only; not an agentic quality benchmark"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=PROJECT / ".state")
    parser.add_argument("--output", type=Path, default=PROJECT / "evidence/multimodal.json")
    main(parser.parse_args())
