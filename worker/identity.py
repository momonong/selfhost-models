"""Pure ASGI middleware; never reads or logs request bodies."""
import uuid


class WorkerIdentity:
    def __init__(self, app):
        self.app = app
        self.epoch = uuid.uuid4().hex.encode()

    async def __call__(self, scope, receive, send):
        async def stamped(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-worker-epoch", self.epoch))
            await send(message)
        await self.app(scope, receive, stamped)
