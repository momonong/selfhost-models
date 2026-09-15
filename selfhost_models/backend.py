"""Small HTTP backend boundary; lifecycle semantics are documented in docs/backend.md."""
import httpx


class VLLMBackend:
    def __init__(self, url: str, max_connections: int):
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
        r = await self.client.post("/v1/chat/completions", json={
            "model": model, "messages": [{"role": "user", "content": "Reply OK."}],
            "max_tokens": 1, "temperature": 0, "stream": False,
        }, timeout=60)
        r.raise_for_status()
        if r.headers.get("x-worker-epoch") != epoch or not r.json().get("choices"):
            raise RuntimeError("warmup identity or result mismatch")

    def generate(self, payload, request_id):
        return self.client.stream("POST", "/v1/chat/completions", json=payload,
                                  headers={"x-request-id": request_id})

    async def close(self):
        await self.client.aclose()
