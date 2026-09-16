"""Single model/process, bounded streaming and execution independent of HTTP life."""
import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import time
import uuid
from dataclasses import dataclass, field

from selfhost_models.capabilities import validate_capabilities
from selfhost_models.schema import Chat


@dataclass
class Work:
    prepared: asyncio.Event = field(default_factory=asyncio.Event)
    chunks: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=16))
    dropped: bool = False
    status: int = 200
    result: dict | None = None
    task: asyncio.Task | None = None

    def emit(self, text):
        try:
            self.chunks.put_nowait(text)
        except queue.Full:
            self.dropped = True  # Keep computing; never block GPU on a lost reader.


class TransformersWorker:
    def __init__(self, engine=None, model=None):
        self.engine = engine
        self.model = model or os.getenv("MODEL_ID", "unconfigured/model")
        self.epoch = uuid.uuid4().hex
        self.loaded = engine is not None
        self.failed = False
        self.active = None
        self.clients = 0

    async def execute(self, work, payload, warmup):
        # Only this owned task releases the slot; handlers never cancel it.
        try:
            try:
                prepared = await asyncio.to_thread(self.engine.prepare, payload)
            except ValueError:
                work.status = 400  # No generation was dispatched.
                return
            work.prepared.set()
            work.result = await asyncio.to_thread(self.engine.run, prepared, payload, work.emit, warmup)
        except Exception as exc:
            logging.error("engine_failed type=%s", type(exc).__name__)
            self.failed = True
            work.status = 500  # No terminal confirmation on engine/CUDA errors.
        finally:
            work.prepared.set()
            self.active = None

    async def start(self, send, status, stream=False):
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"x-worker-epoch", self.epoch.encode()),
            (b"content-type", b"text/event-stream" if stream else b"application/json")]})

    async def response(self, send, status, value):
        await self.start(send, status)
        await send({"type": "http.response.body", "body": json.dumps(value).encode()})

    async def handle(self, scope, receive, send):
        path = scope["path"]
        if path == "/health" and scope["method"] == "GET":
            await self.response(send, 200 if self.loaded and not self.failed else 503,
                                {"loaded": self.loaded, "failed": self.failed, "busy": self.active is not None})
            return
        if path not in ("/v1/chat/completions", "/internal/warmup") or scope["method"] != "POST":
            await self.response(send, 404, {"error": "not_found"})
            return
        if self.clients >= 32:
            await self.response(send, 429, {"error": "too_many_clients"})
            return
        self.clients += 1
        try:
            await self.chat(scope, receive, send, path == "/internal/warmup")
        finally:
            self.clients -= 1

    async def chat(self, scope, receive, send, warmup):
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > 2097152:
                        await self.response(send, 413, {"error": "body_too_large"})
                        return
                    if not event.get("more_body", False):
                        break
            payload = json.loads(body)
            if warmup:
                if payload != {"model": self.model}:
                    raise ValueError("invalid warmup")
                payload = {"model": self.model, "messages": [{"role": "user", "content": "Reply OK."}],
                           "max_tokens": 4, "temperature": 0}
            payload = Chat.model_validate(payload).model_dump(exclude_none=True)
            validate_capabilities(payload, "transformers", "qwen3_5")
        except (ValueError, UnicodeError, RecursionError):
            await self.response(send, 400, {"error": "invalid_request_or_unsupported_capability"})
            return
        except TimeoutError:
            await self.response(send, 408, {"error": "body_timeout"})
            return
        if payload["model"] != self.model:
            await self.response(send, 404, {"error": "model_not_served"})
            return
        if not self.loaded or self.failed:
            await self.response(send, 503, {"error": "worker_not_ready"})
            return
        if self.active is not None:
            await self.response(send, 429, {"error": "overloaded"})
            return
        work = Work()
        self.active = work
        work.task = asyncio.create_task(self.execute(work, payload, warmup))
        # Retain the task via active until it has actually completed. Cancelling
        # this handler or closing its socket leaves inference and slot intact.
        await work.prepared.wait()
        if work.status != 200:
            await self.response(send, work.status, {"error": "worker_rejected" if work.status == 400 else "engine_failed"})
            return
        rid = "chatcmpl-" + uuid.uuid4().hex
        common = {"id": rid, "created": int(time.time()), "model": self.model}
        if not payload["stream"]:
            await asyncio.shield(work.task)
            if work.status != 200:
                await self.response(send, 500, {"error": "engine_failed"})
                return
            result = work.result
            await self.response(send, 200, {**common, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": result["text"]},
                 "finish_reason": result["finish_reason"]}], "usage": result["usage"]})
            return
        await self.start(send, 200, stream=True)

        async def frame(value):
            async with asyncio.timeout(5):
                await send({"type": "http.response.body", "body": ("data: " +
                           (value if isinstance(value, str) else json.dumps(value)) + "\n\n").encode(), "more_body": True})

        def chunk(delta, finish=None):
            return {**common, "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            await frame(chunk({"role": "assistant", "content": ""}))
            while not work.task.done() or not work.chunks.empty():
                if work.dropped:
                    await frame({"error": {"code": "stream_overflow"}})
                    return
                try:
                    text = work.chunks.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                await frame(chunk({"content": text}))
            if work.status != 200 or work.dropped:
                await frame({"error": {"code": "engine_failed"}})
                return
            await frame(chunk({}, work.result["finish_reason"]))
            if payload.get("stream_options", {}).get("include_usage"):
                await frame({**common, "object": "chat.completion.chunk", "choices": [], "usage": work.result["usage"]})
            await frame("[DONE]")  # After generate returned AND CUDA synchronized.
        except (OSError, TimeoutError):
            pass
        finally:
            # Finish HTTP only. The independently owned execution keeps draining.
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await send({"type": "http.response.body", "body": b""})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        if self.engine is None:
                            from .transformers_engine import QwenEngine
                            if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("MODEL_REVISION", "")):
                                raise ValueError("fixed revision required")
                            context = int(os.environ.get("MAX_MODEL_LEN", "2048"))
                            memory = float(os.environ.get("GPU_MEMORY_UTILIZATION", "0.60"))
                            if (os.environ.get("MAX_INFLIGHT", "1") != "1" or not 128 <= context <= 2048
                                    or not 0.1 <= memory <= 0.9):
                                raise ValueError("unsupported deployment limits")
                            self.engine = await asyncio.to_thread(QwenEngine, context=context, memory_fraction=memory)
                        self.loaded = True
                        await send({"type": "lifespan.startup.complete"})
                    except Exception:
                        logging.exception("offline model/runtime load failed")
                        await send({"type": "lifespan.startup.failed", "message": "offline model/runtime load failed"})
                        return
                else:
                    self.loaded = False
                    if self.active:
                        await asyncio.shield(self.active.task)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        elif scope["type"] == "http":
            await self.handle(scope, receive, send)


app = TransformersWorker()
