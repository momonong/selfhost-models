"""Single-process ASGI gateway with durable, conservative worker leases."""
import asyncio
import contextlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from . import __version__
from .backend import VLLMBackend
from .schema import Chat
from .storage import atomic_json

log = logging.getLogger("selfhost")
logging.basicConfig(level=logging.INFO, format="%(message)s")


class Failure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def error(code, rid):
    return {"error": {"message": code.replace("_", " "), "type": code,
                      "code": code, "request_id": rid}}


@dataclass
class Job:
    rid: str
    epoch: str
    stream: bool
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=16))
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    detached: bool = False
    failure: Failure | None = None
    result: bytes = b""


class Gateway:
    def __init__(self, backend=None, *, key=None, state_file=None):
        self.backend = backend
        self.key = key
        self.state_file = Path(state_file or os.getenv("STATE_FILE", ".state/leases.json"))
        self.model = os.getenv("MODEL_ID", "unconfigured/model")
        self.profile = os.getenv("MODEL_PROFILE", "text")
        self.revision = os.getenv("MODEL_REVISION", "unknown")
        self.capacity = int(os.getenv("MAX_INFLIGHT", "2"))
        self.deadline = float(os.getenv("DEADLINE_SECONDS", "30"))
        self.drain = float(os.getenv("DRAIN_SECONDS", "120"))
        self.max_body = int(os.getenv("MAX_BODY_BYTES", "2097152"))
        if not 1 <= self.capacity <= 16 or not 0 < self.deadline <= self.drain <= 600:
            raise ValueError("invalid admission/deadline settings")
        self.ready = False
        self.epoch = None
        self.leases = {}
        self.jobs = {}
        self.tasks = set()
        self.clients = 0
        self.monitor = None

    def save(self):
        atomic_json(self.state_file, {"leases": self.leases})

    async def startup(self):
        if self.key is None:
            self.key = Path(os.environ["API_KEY_FILE"]).read_text().strip()
        if len(self.key) < 24:
            raise ValueError("API key must contain at least 24 characters")
        if self.state_file.exists():
            self.leases = json.loads(self.state_file.read_text())["leases"]
        if self.backend is None:
            self.backend = VLLMBackend(os.getenv("WORKER_URL", "http://worker:8000"), self.capacity + 2,
                                       profile=self.profile, capacity=self.capacity)
        self.monitor = asyncio.create_task(self.watch())

    async def watch(self):
        while True:
            try:
                epoch = await self.backend.identity()
                # Persisted unresolved requests may still run after an API restart.
                unknown = {k: v for k, v in self.leases.items() if k not in self.jobs}
                if any(v == epoch for v in unknown.values()):
                    self.ready = False
                elif self.epoch == epoch:
                    self.ready = True  # This exact engine has already warmed successfully.
                elif self.epoch != epoch or not self.ready:
                    self.ready = False
                    # A new worker frontend owns a newly created engine process.
                    self.leases = {k: v for k, v in self.leases.items() if v == epoch}
                    self.save()
                    # Warmup is also journaled: a lost warmup is unresolved GPU work.
                    warm_id = "warmup-" + uuid.uuid4().hex
                    self.leases[warm_id] = epoch
                    self.save()
                    await self.backend.warmup(self.model, epoch)
                    del self.leases[warm_id]
                    self.save()
                    self.epoch, self.ready = epoch, True
                    log.info(json.dumps({"event": "ready", "version": __version__, "worker_epoch": epoch,
                                         "model": self.model, "revision": self.revision}))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ready = False
                log.info(json.dumps({"event": "worker_not_ready", "error": type(exc).__name__}))
            await asyncio.sleep(2)

    async def produce(self, job, payload):
        terminal = False
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.drain):
                async with self.backend.generate(payload, job.rid) as response:
                    if response.headers.get("x-worker-epoch") != job.epoch:
                        raise Failure(503, "worker_changed")
                    if response.status_code != 200:
                        # Only validation rejection proves no generation was admitted.
                        terminal = response.status_code in (400, 404, 422)
                        raise Failure(400 if terminal else 502, "worker_rejected" if terminal else "worker_failed")
                    if job.stream:
                        job.started.set()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if len(data) > 1048576:
                                raise Failure(502, "worker_response_too_large")
                            if data == "[DONE]":
                                terminal = True
                            else:
                                chunk = json.loads(data)
                                if "error" in chunk:
                                    raise Failure(502, "worker_stream_failed")
                            if not job.detached:
                                try:
                                    job.queue.put_nowait((line + "\n\n").encode())
                                except asyncio.QueueFull:
                                    job.detached = True
                                    job.failure = Failure(408, "slow_consumer")
                            if terminal:
                                break
                        if not terminal:
                            raise Failure(502, "worker_stream_incomplete")
                    else:
                        parts, size = [], 0
                        async for part in response.aiter_bytes():
                            size += len(part)
                            if size > 2097152:
                                raise Failure(502, "worker_response_too_large")
                            parts.append(part)
                        job.result = b"".join(parts)
                        data = json.loads(job.result)
                        if not data.get("choices") or any(c.get("finish_reason") is None for c in data["choices"]):
                            raise Failure(502, "worker_response_incomplete")
                        terminal = True
        except asyncio.CancelledError:
            job.failure = Failure(503, "gateway_shutdown")
            raise
        except Exception as exc:
            job.failure = exc if isinstance(exc, Failure) else Failure(502, "worker_unconfirmed")
        finally:
            if terminal:
                self.leases.pop(job.rid, None)
                self.save()
            else:
                self.ready = False
            self.jobs.pop(job.rid, None)
            job.started.set()
            job.finished.set()
            log.info(json.dumps({"event": "worker_request_end", "request_id": job.rid,
                                 "version": __version__, "seconds": round(time.monotonic() - started, 3),
                                 "terminal_confirmed": terminal, "detached": job.detached,
                                 "error": job.failure.code if job.failure else None}))

    async def json_response(self, send, status, value, rid):
        headers = [(b"content-type", b"application/json"), (b"x-request-id", rid.encode()),
                   (b"x-service-version", __version__.encode())]
        if status in (429, 503):
            headers.append((b"retry-after", b"2"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        await send({"type": "http.response.body", "body": body})

    async def deliver(self, job, send, rid, deadline):
        headers_sent = False
        try:
            async with asyncio.timeout_at(deadline):
                await job.started.wait()
                if job.failure:
                    raise job.failure
                if not job.stream:
                    await self.json_response(send, 200, job.result, rid)
                    return
                await send({"type": "http.response.start", "status": 200, "headers": [
                    (b"content-type", b"text/event-stream"), (b"cache-control", b"no-cache"),
                    (b"x-request-id", rid.encode()), (b"x-service-version", __version__.encode())]})
                headers_sent = True
                while True:
                    if job.failure:
                        raise job.failure
                    if job.finished.is_set() and job.queue.empty():
                        break
                    try:
                        part = await asyncio.wait_for(job.queue.get(), 0.1)
                    except TimeoutError:
                        continue
                    await send({"type": "http.response.body", "body": part, "more_body": True})
                await send({"type": "http.response.body", "body": b""})
        except (TimeoutError, Failure) as exc:
            job.detached = True
            failure = exc if isinstance(exc, Failure) else Failure(504, "deadline_exceeded")
            log.info(json.dumps({"event": "client_error", "request_id": rid, "error": failure.code}))
            # Bound writes too: a non-reading client cannot retain a handler forever.
            with contextlib.suppress(TimeoutError, OSError):
                async with asyncio.timeout(1):
                    if headers_sent:
                        await send({"type": "http.response.body", "body": (
                            "data: " + json.dumps(error(failure.code, rid)) + "\n\n").encode()})
                    else:
                        await self.json_response(send, failure.status, error(failure.code, rid), rid)

    async def chat(self, receive, send, headers, rid, started):
        deadline = self.deadline
        if b"x-request-timeout-ms" in headers:
            try:
                milliseconds = int(headers[b"x-request-timeout-ms"])
            except ValueError:
                raise Failure(400, "invalid_deadline") from None
            if not 1 <= milliseconds <= self.deadline * 1000:
                raise Failure(400, "invalid_deadline")
            deadline = milliseconds / 1000
        if headers.get(b"content-type", b"").split(b";", 1)[0].lower() != b"application/json":
            raise Failure(415, "json_required")
        body = bytearray()
        async with asyncio.timeout(min(10, deadline)):
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    return
                body.extend(event.get("body", b""))
                if len(body) > self.max_body:
                    raise Failure(413, "body_too_large")
                if not event.get("more_body", False):
                    break
        try:
            # Validate Python objects so strict integers/bools do not get coerced.
            payload = Chat.model_validate(json.loads(body)).model_dump(exclude_none=True)
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            raise Failure(400, "invalid_request") from None
        if payload["model"] != self.model:
            raise Failure(404, "model_not_served")
        has_images = any(isinstance(m.get("content"), list) and any(p["type"] == "image_url" for p in m["content"])
                         for m in payload["messages"])
        has_tools = payload.get("tools") or any(m.get("tool_calls") or m["role"] == "tool" for m in payload["messages"])
        if self.profile not in ("qwen3_5", "gemma4") and (has_images or has_tools or "chat_template_kwargs" in payload):
            raise Failure(400, "unsupported_model_capability")
        if self.profile == "gemma4" and "chat_template_kwargs" in payload:
            raise Failure(400, "unsupported_model_capability")
        if self.profile == "qwen3_5":
            payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
        if not self.ready:
            raise Failure(503, "worker_not_ready")
        if len(self.leases) >= self.capacity:
            raise Failure(429, "overloaded")
        job = Job(rid, self.epoch, payload["stream"])
        self.leases[rid] = job.epoch
        self.save()  # Must be durable BEFORE sending to worker.
        self.jobs[rid] = job
        task = asyncio.create_task(self.produce(job, payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        async def disconnect():
            while (await receive())["type"] != "http.disconnect":
                pass

        delivery = asyncio.create_task(self.deliver(job, send, rid, started + deadline))
        gone = asyncio.create_task(disconnect())
        try:
            done, _ = await asyncio.wait([delivery, gone], return_when=asyncio.FIRST_COMPLETED)
            if gone in done:
                job.detached = True
                log.info(json.dumps({"event": "client_disconnected", "request_id": rid}))
            else:
                await delivery
        finally:
            # Deliberately do NOT cancel the producer: it owns the GPU lease.
            if not job.finished.is_set():
                job.detached = True
            for t in (delivery, gone):
                t.cancel()
            await asyncio.gather(delivery, gone, return_exceptions=True)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        await self.startup()
                        await send({"type": "lifespan.startup.complete"})
                    except Exception:
                        await send({"type": "lifespan.startup.failed", "message": "gateway configuration/state invalid"})
                        return
                else:
                    for task in [self.monitor, *self.tasks]:
                        task.cancel()
                    await asyncio.gather(self.monitor, *self.tasks, return_exceptions=True)
                    await self.backend.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        rid, started = uuid.uuid4().hex, time.monotonic()
        headers = dict(scope["headers"])
        try:
            path, method = scope["path"], scope["method"]
            if path in ("/health/live", "/health/ready") and method == "GET":
                await self.json_response(send, 200 if path.endswith("live") or self.ready else 503,
                    {"ready": self.ready, "version": __version__, "worker_epoch": self.epoch,
                     "inflight": len(self.leases), "detached": sum(j.detached for j in self.jobs.values()),
                     "uncertain": sum(k not in self.jobs for k in self.leases)}, rid)
                return
            if not self.key or not hmac.compare_digest(headers.get(b"authorization", b""), ("Bearer " + self.key).encode()):
                raise Failure(401, "unauthorized")
            if self.clients >= 32:
                raise Failure(429, "too_many_clients")
            self.clients += 1
            try:
                if path == "/v1/models" and method == "GET":
                    if not self.ready:
                        raise Failure(503, "worker_not_ready")
                    await self.json_response(send, 200, {"object": "list", "data": [{
                        "id": self.model, "object": "model", "created": 0, "owned_by": "selfhost",
                        "revision": self.revision}]}, rid)
                elif path == "/v1/chat/completions" and method == "POST":
                    await self.chat(receive, send, headers, rid, started)
                else:
                    raise Failure(404, "not_found")
            finally:
                self.clients -= 1
        except (Failure, TimeoutError) as exc:
            failure = exc if isinstance(exc, Failure) else Failure(408, "body_timeout")
            await self.json_response(send, failure.status, error(failure.code, rid), rid)
        finally:
            log.info(json.dumps({"event": "http_end", "request_id": rid, "version": __version__,
                                 "seconds": round(time.monotonic() - started, 3)}))


app = Gateway()
