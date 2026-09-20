"""CPU ingress relay. GPU worker stays on an internal-only Docker network.

This fixed bridge accepts an independent internal key and a small operation
allowlist. It never selects a URL, command, or model from client input.
"""
import asyncio
import hmac
import os
import re
from pathlib import Path

import httpx


class Relay:
    def __init__(self, target, key):
        if not re.fullmatch(r"http://selfhost-scheduler-[a-z0-9-]+:8000", target) or len(key) < 32:
            raise ValueError("invalid relay configuration")
        self.target, self.key = target, key
        self.clients, self.bytes = 0, 0
        self.client = httpx.AsyncClient(base_url=target, trust_env=False,
            timeout=httpx.Timeout(5, read=None), limits=httpx.Limits(max_connections=20))

    async def error(self, send, status):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type",b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"error":"relay_rejected"}'})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event=await receive()
                if event["type"]=="lifespan.startup":
                    await send({"type":"lifespan.startup.complete"})
                else:
                    await self.client.aclose()
                    await send({"type":"lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http": return
        headers=dict(scope.get("headers",[]))
        if not hmac.compare_digest(headers.get(b"x-selfhost-worker-key",b""),self.key.encode()):
            await self.error(send,401);return
        if (scope["method"],scope["path"]) not in {("GET","/health"),("POST","/v1/chat/completions"),("POST","/internal/transcribe"),("POST","/internal/warmup")}:
            await self.error(send,404);return
        if self.clients>=18:
            await self.error(send,429);return
        body=bytearray();self.clients+=1;started=False
        try:
            async with asyncio.timeout(10):
                while True:
                    event=await receive()
                    if event["type"]=="http.disconnect": return
                    chunk=event.get("body",b"")
                    if len(body)+len(chunk)>32*1024**2 or self.bytes+len(chunk)>64*1024**2:
                        await self.error(send,413);return
                    body.extend(chunk);self.bytes+=len(chunk)
                    if not event.get("more_body"):break
            async with self.client.stream(scope["method"],scope["path"],content=bytes(body),
                    headers={"content-type":"application/json","x-selfhost-worker-key":self.key}) as response:
                result_headers=[(b"content-type",response.headers.get("content-type","application/json").encode())]
                if response.headers.get("x-worker-epoch"):
                    result_headers.append((b"x-worker-epoch",response.headers["x-worker-epoch"].encode()))
                await send({"type":"http.response.start","status":response.status_code,"headers":result_headers})
                started=True
                async for chunk in response.aiter_bytes():
                    await send({"type":"http.response.body","body":chunk,"more_body":True})
                await send({"type":"http.response.body","body":b""})
        except Exception:
            if not started: await self.error(send,503)
            else: await send({"type":"http.response.body","body":b""})  # no invented terminal or SSE DONE
        finally:
            self.bytes-=len(body);self.clients-=1


def create_app():
    return Relay(os.environ["RELAY_WORKER_URL"],Path(os.environ["WORKER_API_KEY_FILE"]).read_text().strip())


if __name__=="__main__":
    import uvicorn
    uvicorn.run(create_app(),host="0.0.0.0",port=8000,workers=1,access_log=False)
