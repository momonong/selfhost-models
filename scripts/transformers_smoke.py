"""Synthetic real-GPU text checks beyond the common service lifecycle suite."""
import argparse
import base64
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def main(args):
    rows = []
    with httpx.Client(base_url=args.url, trust_env=False, timeout=65,
                     headers={"Authorization": "Bearer " + (args.state / "api-key").read_text().strip()}) as client:
        model = client.get("/v1/models").json()["data"][0]
        assert model["backend"] == "transformers"
        assert model["max_inflight"] == 1 and not model["capabilities"]["images"] and not model["capabilities"]["tools"]
        payload = {"model": model["id"], "messages": [{"role": "user", "content": "What is 2 plus 2? Reply with only the number."}],
                   "max_tokens": 32, "temperature": 0}
        response = client.post("/v1/chat/completions", json=payload)
        assert response.status_code == 200, response.text
        answer = response.json()["choices"][0]["message"]["content"]
        assert "4" in answer, answer
        rows.append({"check": "synthetic_arithmetic", "answer": answer, "usage": response.json()["usage"]})
        parts, terminal, usage = [], False, None
        with client.stream("POST", "/v1/chat/completions", json={**payload, "stream": True,
                           "stream_options": {"include_usage": True}}) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    terminal = True
                else:
                    chunk = json.loads(data)
                    assert "error" not in chunk
                    for choice in chunk["choices"]:
                        parts.append(choice["delta"].get("content", ""))
                    usage = chunk.get("usage", usage)
        assert terminal and "".join(parts) == answer and usage["completion_tokens"] > 0
        rows.append({"check": "greedy_json_sse_content_matches", "done": terminal, "usage": usage})
        sample = {**payload, "temperature": 0.7, "top_p": 0.8, "seed": 123,
                  "messages": [{"role": "user", "content": [{"type": "text", "text": "Name a common fruit."}]}]}
        outputs = []
        for _ in range(2):
            response = client.post("/v1/chat/completions", json=sample)
            assert response.status_code == 200, response.text
            outputs.append(response.json()["choices"][0]["message"]["content"])
        assert outputs[0] == outputs[1]
        rows.append({"check": "sampling_text_parts_seed_repeat_same_runtime", "outputs": outputs})
        png = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(png, format="PNG")
        image = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode()
        for name, overrides in [
            ("stop", {"stop": "END"}), ("presence_penalty", {"presence_penalty": 0.5}),
            ("frequency_penalty", {"frequency_penalty": 0.5}),
            ("thinking", {"chat_template_kwargs": {"enable_thinking": True}}),
            ("top_p_with_greedy", {"top_p": 0.8}),
            ("image", {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": image}}]}]}),
            ("tools", {"tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]})]:
            response = client.post("/v1/chat/completions", json={**payload, **overrides})
            assert response.status_code == 400, (name, response.text)
            rows.append({"check": "reject_" + name, "status": response.status_code, "code": response.json()["error"]["code"]})
        health = client.get("/health/ready").json()
        assert health["ready"] and health["inflight"] == health["uncertain"] == 0
    evidence = {"at": datetime.now(timezone.utc).isoformat(), "level": "real-GPU-synthetic-inputs",
                "model": model, "results": rows, "final_health": health}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/transformers-smoke.json")
    main(parser.parse_args())
