"""Pure ASGI middleware; never reads or logs request bodies."""
import uuid
import hmac
import os
from pathlib import Path


class WorkerIdentity:
    def __init__(self, app):
        self.app = app
        self.epoch = uuid.uuid4().hex.encode()
        key_file = os.getenv("WORKER_API_KEY_FILE")
        self.key = Path(key_file).read_text().strip().encode() if key_file else None
        if self.key is not None and len(self.key) < 32:
            raise ValueError("invalid worker key")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.key is not None:
            headers = dict(scope.get("headers", []))
            valid = hmac.compare_digest(headers.get(b"x-selfhost-worker-key", b""), self.key)
            allowed = (scope.get("method"), scope.get("path")) in {
                ("GET", "/health"), ("POST", "/v1/chat/completions"),
                ("POST", "/internal/transcribe"), ("POST", "/internal/warmup")}
            if not valid or not allowed:
                await send({"type": "http.response.start", "status": 401 if not valid else 404,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"worker_access_denied"}'})
                return
        async def stamped(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-worker-epoch", self.epoch))
            await send(message)
        await self.app(scope, receive, stamped)
