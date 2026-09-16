import asyncio
import contextlib
import json

import httpx
import pytest

from selfhost_models.api import Gateway, Job
from selfhost_models.schema import Chat

KEY = "test-key-" + "x" * 32
PAYLOAD = {"model": "org/model", "messages": [{"role": "user", "content": "synthetic"}]}


class FakeBackend:
    def __init__(self):
        self.finish = asyncio.Event()
        self.entered = asyncio.Event()
        self.epoch = "engine-one"
        self.fail = False

    async def identity(self):
        return self.epoch

    async def warmup(self, model, epoch):
        pass

    async def close(self):
        pass

    @contextlib.asynccontextmanager
    async def generate(self, payload, rid):
        self.entered.set()
        await self.finish.wait()
        if self.fail:
            raise httpx.ReadError("do not expose input")
        data = {"id": rid, "choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        yield httpx.Response(200, headers={"x-worker-epoch": self.epoch}, json=data)


@pytest.fixture(params=["vllm", "transformers"])
def app(tmp_path, monkeypatch, request):
    monkeypatch.setenv("BACKEND", request.param)
    monkeypatch.setenv("MODEL_PROFILE", "qwen3_5")
    monkeypatch.setenv("MAX_INFLIGHT", "1")
    app = Gateway(FakeBackend(), key=KEY, state_file=tmp_path / "leases.json")
    app.model, app.epoch, app.ready = "org/model", "engine-one", True
    app.capacity = 1
    return app


async def call(app, payload=PAYLOAD, path="/v1/chat/completions", method="POST", key=KEY):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        return await c.request(method, path, json=payload, headers={"Authorization": f"Bearer {key}"})


async def test_validation_no_forward(app):
    for bad in ({**PAYLOAD, "tools": []}, {**PAYLOAD, "max_tokens": 0}, {**PAYLOAD, "stream": "true"}):
        assert (await call(app, bad)).status_code == 400
    assert not app.backend.entered.is_set()
    assert (await call(app, key="wrong")).status_code == 401
    assert (await call(app, {**PAYLOAD, "model": "org/other"})).status_code == 404
    app.ready = False
    assert (await call(app)).status_code == 503


async def test_deadline_retains_lease_and_overload(app):
    app.deadline = 0.05
    response = await call(app)
    assert response.status_code == 504
    assert len(app.leases) == 1
    assert (await call(app)).status_code == 429
    assert json.loads(app.state_file.read_text())["leases"]
    app.backend.finish.set()
    await asyncio.gather(*app.tasks)
    assert not app.leases
    assert (await call(app)).status_code == 200


async def test_disconnect_keeps_gpu_lease(app):
    events = asyncio.Queue()
    await events.put({"type": "http.request", "body": json.dumps(PAYLOAD).encode()})
    sent = []
    async def send(x):
        sent.append(x)
    task = asyncio.create_task(app({"type": "http", "method": "POST", "path": "/v1/chat/completions",
        "headers": [(b"authorization", ("Bearer " + KEY).encode()), (b"content-type", b"application/json")]}, events.get, send))
    await app.backend.entered.wait()
    await events.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 1)
    assert len(app.leases) == 1
    assert next(iter(app.jobs.values())).detached
    app.backend.finish.set()
    await asyncio.gather(*app.tasks)
    assert not app.leases


async def test_uncertain_lease_survives_api_restart(app):
    app.backend.fail = True
    app.backend.finish.set()
    assert (await call(app)).status_code == 502
    assert not app.ready and app.leases
    other = Gateway(app.backend, key=KEY, state_file=app.state_file)
    await other.startup()
    await asyncio.sleep(0.02)
    assert not other.ready and other.leases
    other.backend.epoch = "engine-two"
    await asyncio.sleep(2.05)
    assert other.ready and not other.leases
    other.monitor.cancel()
    await asyncio.gather(other.monitor, return_exceptions=True)


def test_schema_rejects_unknown_nested_fields():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Chat.model_validate({**PAYLOAD, "messages": [{"role": "user", "content": "x", "secret": "x"}]})


async def test_receiving_handlers_are_bounded_before_admission(app):
    receiving = []
    async def blocked_receive():
        await asyncio.Event().wait()
    async def send(event):
        pass
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions",
             "headers": [(b"authorization", ("Bearer " + KEY).encode()), (b"content-type", b"application/json")]}
    try:
        for _ in range(32):
            receiving.append(asyncio.create_task(app(scope, blocked_receive, send)))
        await asyncio.sleep(0)
        assert app.clients == 32 and not app.leases
        response = await call(app)
        assert response.status_code == 429 and response.json()["error"]["code"] == "too_many_clients"
    finally:
        for task in receiving:
            task.cancel()
        await asyncio.gather(*receiving, return_exceptions=True)
    assert app.clients == 0


async def test_backend_capabilities_advertised_and_rejected_without_dispatch(app):
    response = await call(app, path="/v1/models", method="GET")
    model = response.json()["data"][0]
    assert model["backend"] == app.backend_name
    assert model["capabilities"]["cancellation"] == "drain_to_terminal"
    if app.backend_name == "transformers":
        assert not model["capabilities"]["images"] and not model["capabilities"]["tools"]
        for options in ({"stop": "END"}, {"presence_penalty": 1.0}, {"chat_template_kwargs": {"enable_thinking": True}}):
            assert (await call(app, {**PAYLOAD, **options})).status_code == 400
        assert not app.backend.entered.is_set()
