"""CPU engine doubles exercise the actual HTTP worker and its execution owner."""
import asyncio
import json
import threading

import httpx
import pytest

from worker.transformers_app import TransformersWorker, Work

PAYLOAD = {"model": "org/model", "messages": [{"role": "user", "content": "synthetic"}], "max_tokens": 4}


class Engine:
    def __init__(self):
        self.started = threading.Event()
        self.finish = threading.Event()
        self.reject = False
        self.fail = False
        self.calls = 0

    def prepare(self, payload):
        if self.reject:
            raise ValueError("context_limit")
        return payload

    def run(self, prepared, payload, emit, warmup):
        self.calls += 1
        self.started.set()
        emit("OK")
        if not self.finish.wait(5):
            raise RuntimeError("test hung")
        if self.fail:
            raise RuntimeError("sensitive engine diagnostic")
        return {"text": "OK", "finish_reason": "length", "usage": {
            "prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def invoke(app, payload=PAYLOAD, path="/v1/chat/completions"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://worker") as c:
        return await c.post(path, json=payload)


async def test_cancelled_http_does_not_release_or_stop_engine():
    engine = Engine()
    app = TransformersWorker(engine, "org/model")
    pending = asyncio.create_task(invoke(app))
    await wait_until(engine.started.is_set)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert app.active is not None
    assert (await invoke(app)).status_code == 429
    assert engine.calls == 1
    engine.finish.set()
    await wait_until(lambda: app.active is None)
    assert (await invoke(app)).status_code == 200


async def test_sse_done_only_after_actual_engine_return():
    engine = Engine()
    app = TransformersWorker(engine, "org/model")
    sent = []
    events = asyncio.Queue()
    await events.put({"type": "http.request", "body": json.dumps({**PAYLOAD, "stream": True,
                           "stream_options": {"include_usage": True}}).encode()})
    async def send(event):
        sent.append(event)
    pending = asyncio.create_task(app({"type": "http", "method": "POST", "path": "/v1/chat/completions"}, events.get, send))
    await wait_until(lambda: any(b"OK" in e.get("body", b"") for e in sent))
    assert app.active is not None
    assert not any(b"[DONE]" in e.get("body", b"") for e in sent)
    engine.finish.set()
    await pending
    body = b"".join(e.get("body", b"") for e in sent)
    assert b'"finish_reason": "length"' in body and b'"completion_tokens": 4' in body
    assert body.endswith(b"data: [DONE]\n\n")
    assert not app.active


async def test_validation_rejection_is_pre_execution_and_engine_failure_is_unhealthy():
    engine = Engine()
    app = TransformersWorker(engine, "org/model")
    engine.reject = True
    response = await invoke(app)
    assert response.status_code == 400 and engine.calls == 0 and not app.failed
    engine.reject, engine.fail = False, True
    engine.finish.set()
    response = await invoke(app)
    assert response.status_code == 500 and "sensitive" not in response.text
    assert app.failed
    assert (await invoke(app)).status_code == 503


@pytest.mark.parametrize("options", [{"stop": "END"}, {"presence_penalty": 1.0}, {"frequency_penalty": 1.0},
    {"chat_template_kwargs": {"enable_thinking": True}}, {"logprobs": True}])
async def test_unsupported_parameters_rejected_before_engine(options):
    engine = Engine()
    app = TransformersWorker(engine, "org/model")
    assert (await invoke(app, {**PAYLOAD, **options})).status_code == 400
    assert engine.calls == 0 and not app.active


async def test_warmup_terminal_and_identity():
    engine = Engine()
    engine.finish.set()
    app = TransformersWorker(engine, "org/model")
    response = await invoke(app, {"model": "org/model"}, "/internal/warmup")
    assert response.status_code == 200
    assert response.headers["x-worker-epoch"] == app.epoch
    assert response.json()["usage"]["completion_tokens"] == 4
    assert TransformersWorker(engine, "org/model").epoch != app.epoch


def test_unread_stream_buffer_is_bounded_and_does_not_block_engine():
    work = Work()
    for _ in range(1000):
        work.emit("token")
    assert work.chunks.qsize() == 16 and work.dropped
