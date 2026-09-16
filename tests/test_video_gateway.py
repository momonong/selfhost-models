import asyncio
import copy
import json

import pytest
from pydantic import ValidationError

from selfhost_models import api
from selfhost_models.schema import Chat
from selfhost_models.video import PREFIX, VideoError
from test_gateway import FakeBackend, KEY, call
from test_stream import StreamingBackend

PAYLOAD = {"model": "Qwen/Qwen3.5-4B", "messages": [{"role": "user", "content": [
    {"type": "text", "text": "Describe the sequence."},
    {"type": "video_url", "video_url": {"url": PREFIX + "eA=="}}]}]}
META = {"timestamps_seconds": [0.0, 1.0], "sampled_frames": 2, "audio_processed": False}


@pytest.fixture
def video_app(tmp_path, monkeypatch):
    for key, value in {"BACKEND": "vllm", "MODEL_PROFILE": "qwen3_5", "VIDEO_ENABLED": "1",
                       "MODEL_ID": PAYLOAD["model"], "MODEL_REVISION": "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
                       "MAX_MODEL_LEN": "8192", "MAX_INFLIGHT": "1"}.items():
        monkeypatch.setenv(key, value)
    app = api.Gateway(FakeBackend(), key=KEY, state_file=tmp_path / "leases.json")
    app.epoch, app.ready = "engine-one", True
    return app


@pytest.mark.parametrize("uri", ["https://example.com/a.mp4", "file:///a.mp4", "D:/a.mp4", "data:video/jpeg;base64,eA=="])
def test_video_schema_rejects_fetches_and_internal_representation(uri):
    payload = copy.deepcopy(PAYLOAD)
    payload["messages"][0]["content"][1]["video_url"]["url"] = uri
    with pytest.raises(ValidationError):
        Chat.model_validate(payload)


def test_video_schema_rejects_multiple_or_mixed_media():
    from test_capabilities import image_uri
    for part in (PAYLOAD["messages"][0]["content"][1], {"type": "image_url", "image_url": {"url": image_uri()}}):
        payload = copy.deepcopy(PAYLOAD)
        payload["messages"][0]["content"].append(part)
        with pytest.raises(ValidationError):
            Chat.model_validate(payload)


async def test_admission_precedes_decoder_and_deadline_skips_gpu(video_app, monkeypatch):
    app = video_app
    entered, finish = asyncio.Event(), asyncio.Event()
    async def prepare(payload):
        assert app.leases and json.loads(app.state_file.read_text())["leases"]
        entered.set()
        await finish.wait()
        return META
    monkeypatch.setattr(api, "prepare_video", prepare)
    app.ready = False
    assert (await call(app, PAYLOAD)).status_code == 503
    assert not entered.is_set()
    app.ready, app.deadline = True, 0.03
    assert (await call(app, PAYLOAD)).status_code == 504
    assert entered.is_set() and app.leases and not app.backend.entered.is_set()
    assert (await call(app, PAYLOAD)).status_code == 429
    finish.set()
    await asyncio.gather(*app.tasks)
    assert not app.leases and not app.backend.entered.is_set()


async def test_decoder_rejection_releases_only_after_cleanup(video_app, monkeypatch):
    async def reject(payload):
        assert video_app.leases
        raise VideoError(400, "invalid_or_unsupported_video")
    monkeypatch.setattr(api, "prepare_video", reject)
    response = await call(video_app, PAYLOAD)
    assert response.status_code == 400
    assert not video_app.leases and video_app.ready and not video_app.backend.entered.is_set()


@pytest.mark.parametrize("stream", [False, True])
async def test_video_metadata_json_and_sse(video_app, monkeypatch, stream):
    async def prepare(payload):
        return META
    monkeypatch.setattr(api, "prepare_video", prepare)
    if stream:
        video_app.backend, video_app.epoch = StreamingBackend(), "one"
    else:
        video_app.backend.finish.set()
    response = await call(video_app, {**PAYLOAD, "stream": stream})
    assert response.status_code == 200
    if stream:
        frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert json.loads(frames[0])["video"] == META and frames[-1] == "[DONE]"
    else:
        assert response.json()["video"] == META
    assert not video_app.leases


async def test_disabled_video_and_transformers_reject_before_decode(video_app, monkeypatch):
    async def forbidden(payload):
        raise AssertionError("must not decode unsupported video")
    monkeypatch.setattr(api, "prepare_video", forbidden)
    video_app.video_enabled = False
    for backend in ("vllm", "transformers"):
        video_app.backend_name = backend
        assert (await call(video_app, PAYLOAD)).status_code == 400
    assert not video_app.leases


async def test_receive_budget_and_disconnect_return_memory(video_app):
    app = video_app
    app.receive_budget = 100
    events = asyncio.Queue()
    await events.put({"type": "http.request", "body": b" " * 80, "more_body": True})
    async def send(event):
        pass
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": [
        (b"authorization", ("Bearer " + KEY).encode()), (b"content-type", b"application/json")]}
    task = asyncio.create_task(app(scope, events.get, send))
    await asyncio.sleep(0)
    assert app.receiving_bytes == 80
    response = await call(app, PAYLOAD)
    assert response.status_code == 429 and response.json()["error"]["code"] == "receive_budget_exceeded"
    assert app.receiving_bytes == 80
    await events.put({"type": "http.disconnect"})
    await task
    assert app.receiving_bytes == 0 and not app.leases


async def test_preparation_does_not_consume_gpu_drain_budget(video_app, monkeypatch):
    app = video_app
    app.deadline, app.drain = 1, 0.15
    async def prepare(payload):
        await asyncio.sleep(.12)
        return META
    async def finish_worker():
        await app.backend.entered.wait()
        await asyncio.sleep(.08)
        app.backend.finish.set()
    monkeypatch.setattr(api, "prepare_video", prepare)
    finish = asyncio.create_task(finish_worker())
    response = await call(app, PAYLOAD)
    await finish
    assert response.status_code == 200 and not app.leases
