"""Small HTTP backend boundary; lifecycle semantics are documented in docs/backend.md."""
import asyncio
import base64
import io

import httpx


class VLLMBackend:
    def __init__(self, url: str, max_connections: int, profile="text", capacity=2):
        self.profile, self.capacity = profile, capacity
        self.client = httpx.AsyncClient(
            base_url=url, trust_env=False,
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections),
            timeout=httpx.Timeout(5, read=None, pool=1),
        )

    async def identity(self):
        r = await self.client.get("/health", timeout=2)
        r.raise_for_status()
        epoch = r.headers.get("x-worker-epoch")
        if not epoch:
            raise RuntimeError("worker identity missing")
        return epoch

    async def warmup(self, model: str, epoch: str):
        payload = {
            "model": model, "messages": [{"role": "user", "content": "Reply OK."}],
            "max_tokens": 4, "min_tokens": 4, "ignore_eos": True, "temperature": 0, "stream": False,
        }
        if self.profile == "qwen3_5":
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        async def generate(body):
            r = await self.client.post("/v1/chat/completions", json=body, timeout=180)
            r.raise_for_status()
            if r.headers.get("x-worker-epoch") != epoch or not r.json().get("choices"):
                raise RuntimeError("warmup identity or result mismatch")

        # Startup budget is separate from client deadline. Every warmup remains
        # covered by the gateway's durable warmup lease until all have completed.
        async with asyncio.timeout(180):
            await generate(payload)  # Prefill AND recurrent decode, not only one token.
            if self.capacity > 1:
                await asyncio.gather(*(generate(payload) for _ in range(self.capacity)))
            if self.profile in ("qwen3_5", "gemma4"):
                from PIL import Image
                png = io.BytesIO()
                Image.new("RGB", (224, 224), "red").save(png, format="PNG")
                visual = {**payload, "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Describe the color."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
                     base64.b64encode(png.getvalue()).decode()}}]}]}
                await generate(visual)

    def generate(self, payload, request_id):
        return self.client.stream("POST", "/v1/chat/completions", json=payload,
                                  headers={"x-request-id": request_id})

    async def close(self):
        await self.client.aclose()


class TransformersBackend(VLLMBackend):
    """Same transport/terminal contract, with a text-only fixed-length warmup."""

    async def warmup(self, model: str, epoch: str):
        async with asyncio.timeout(180):
            response = await self.client.post("/internal/warmup", json={"model": model}, timeout=180)
            response.raise_for_status()
            result = response.json()
            if (response.headers.get("x-worker-epoch") != epoch or
                    not result.get("choices") or
                    any(c.get("finish_reason") is None for c in result["choices"]) or
                    result.get("usage", {}).get("completion_tokens", 0) < 4):
                raise RuntimeError("warmup identity or terminal result mismatch")


def create_backend(name, *args, **kwargs):
    if name == "vllm":
        return VLLMBackend(*args, **kwargs)
    if name == "transformers":
        return TransformersBackend(*args, **kwargs)
    raise ValueError("unsupported backend")
