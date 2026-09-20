"""Bounded ASR worker; owned compute task survives HTTP cancellation."""
import asyncio
import base64
import io
import json
import os
import wave
from typing import Annotated, Literal

from pydantic import Field

from selfhost_models.schema import StrictModel
from selfhost_models.wav import validate_wav
from worker.identity import WorkerIdentity
from worker.whisper_engine import MODEL, REVISION, WhisperEngine


class Request(StrictModel):
    model: Literal["openai/whisper-small"]
    audio_base64: Annotated[str, Field(max_length=1398104)]
    language: Literal["en", "zh", "ja", "ko", "de", "fr", "es"] | None = None
    max_tokens: Annotated[int, Field(ge=1, le=256)] = 128


class WhisperWorker:
    def __init__(self, engine=None):
        self.engine = engine
        self.failed = False
        self.active = None
        self.clients = 0

    async def reply(self, send, status, data):
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(data).encode()})

    async def execute(self, raw, language, max_tokens, warmup):
        try:
            result = await asyncio.to_thread(self.engine.run, raw, language, max_tokens, warmup)
            if result.get("terminal") is not True:
                raise RuntimeError("terminal missing")
            return 200, result
        except Exception:
            self.failed = True
            return 503, {"error": "engine_failed"}
        finally:
            self.active = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    try:
                        if self.engine is None:
                            self.engine = await asyncio.to_thread(WhisperEngine, "/models/current",
                                os.getenv("MODEL_ID"), os.getenv("MODEL_REVISION"), float(os.getenv("GPU_MEMORY_UTILIZATION", "0.17")))
                        await send({"type": "lifespan.startup.complete"})
                    except Exception:
                        await send({"type": "lifespan.startup.failed", "message": "Whisper initialization failed"})
                        return
                else:
                    if self.active:
                        await asyncio.shield(self.active)
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"] == "/health" and scope["method"] == "GET":
            await self.reply(send, 200 if self.engine and not self.failed else 503,
                             {"loaded": self.engine is not None, "failed": self.failed, "busy": self.active is not None})
            return
        if scope["method"] != "POST" or scope["path"] not in ("/internal/transcribe", "/internal/warmup"):
            await self.reply(send, 404, {"error": "not_found"})
            return
        if self.clients >= 4:
            await self.reply(send, 429, {"error": "overloaded"})
            return
        self.clients += 1
        try:
            body = bytearray()
            async with asyncio.timeout(10):
                while True:
                    event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > 1500000:
                        await self.reply(send, 413, {"error": "body_too_large"})
                        return
                    if not event.get("more_body"):
                        break
            warmup = scope["path"] == "/internal/warmup"
            value = json.loads(body)
            if warmup:
                if value != {"model": MODEL}:
                    raise ValueError("invalid warmup")
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(bytes(960000))
                raw, language, max_tokens = buf.getvalue(), "en", 4
            else:
                req = Request.model_validate(value)
                raw = base64.b64decode(req.audio_base64, validate=True)
                validate_wav(raw)
                language, max_tokens = req.language, req.max_tokens
            if not self.engine or self.failed:
                await self.reply(send, 503, {"error": "not_ready"})
                return
            if self.active is not None:
                await self.reply(send, 429, {"error": "overloaded"})
                return
            self.active = asyncio.create_task(self.execute(raw, language, max_tokens, warmup))
            status, result = await asyncio.shield(self.active)
            await self.reply(send, status, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.reply(send, 400, {"error": "invalid_request"})
        finally:
            self.clients -= 1


app = WorkerIdentity(WhisperWorker())
