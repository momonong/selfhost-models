import asyncio
import base64
import json
from pathlib import Path

import pytest

from selfhost_models import video


def payload(raw=b"synthetic mp4 placeholder"):
    return {"messages": [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": video.PREFIX + base64.b64encode(raw).decode()}}
    ]}]}


class Process:
    def __init__(self):
        self.returncode = None
        self.stopped = asyncio.Event()
        self.killed = False
        self.reaped = False

    async def wait(self):
        await self.stopped.wait()
        self.reaped = True
        return self.returncode

    def kill(self):
        self.killed, self.returncode = True, -9
        self.stopped.set()


@pytest.fixture
def child(monkeypatch):
    monkeypatch.setattr(video, "LINUX_DECODER", True)
    process = Process()
    launched = asyncio.Event()
    async def spawn(*args, **kwargs):
        process.source, process.target = Path(args[-2]), Path(args[-1])
        assert process.source.read_bytes() == b"synthetic mp4 placeholder"
        assert kwargs["stdout"] == kwargs["stderr"] == asyncio.subprocess.DEVNULL
        assert "API_KEY_FILE" not in kwargs["env"]
        launched.set()
        return process
    monkeypatch.setattr(video.asyncio, "create_subprocess_exec", spawn)
    return process, launched


async def test_timeout_reaps_child_before_removing_temporary_files(tmp_path, monkeypatch, child):
    process, launched = child
    monkeypatch.setattr(video, "DECODE_SECONDS", 0.02)
    with pytest.raises(video.VideoError) as exc:
        await video.prepare_video(payload(), temp_root=tmp_path)
    assert exc.value.code == "video_decode_timeout"
    assert process.killed and process.reaped
    assert not list(tmp_path.iterdir())


async def test_shutdown_reaps_cpu_child_and_cleans_files(tmp_path, child):
    process, launched = child
    task = asyncio.create_task(video.prepare_video(payload(), temp_root=tmp_path))
    await launched.wait()
    assert process.source.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed and process.reaped
    assert not list(tmp_path.iterdir())


async def test_rejected_decoder_cleans_without_forwarding(tmp_path, child):
    process, launched = child
    request = payload()
    original = json.loads(json.dumps(request))
    task = asyncio.create_task(video.prepare_video(request, temp_root=tmp_path))
    await launched.wait()
    process.target.write_text(json.dumps({"error": "invalid_or_unsupported_video"}))
    process.returncode = 0
    process.stopped.set()
    with pytest.raises(video.VideoError) as exc:
        await task
    assert exc.value.code == "invalid_or_unsupported_video" and process.reaped
    assert request == original and not list(tmp_path.iterdir())


async def test_invalid_base64_never_starts_decoder(tmp_path, child):
    process, launched = child
    request = payload()
    request["messages"][0]["content"][0]["video_url"]["url"] = video.PREFIX + "!"
    with pytest.raises(video.VideoError) as exc:
        await video.prepare_video(request, temp_root=tmp_path)
    assert exc.value.code == "invalid_video_base64"
    assert not launched.is_set() and not list(tmp_path.iterdir())
