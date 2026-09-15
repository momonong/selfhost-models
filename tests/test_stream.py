import asyncio
import contextlib
import json

import httpx

from selfhost_models.api import Gateway, Job


class Chunks(httpx.AsyncByteStream):
    def __init__(self, terminal=True, count=3):
        self.terminal, self.count = terminal, count

    async def __aiter__(self):
        for i in range(self.count):
            yield ('data: ' + json.dumps({"choices": [{"delta": {"content": str(i)}}]}) + '\n\n').encode()
        if self.terminal:
            yield b'data: [DONE]\n\n'


class StreamingBackend:
    def __init__(self, terminal=True, count=3):
        self.terminal, self.count = terminal, count

    @contextlib.asynccontextmanager
    async def generate(self, payload, rid):
        yield httpx.Response(200, headers={"x-worker-epoch": "one"}, stream=Chunks(self.terminal, self.count))


async def test_stream_done_releases(tmp_path):
    app = Gateway(StreamingBackend(), key="x" * 32, state_file=tmp_path / "state.json")
    job = Job("id", "one", True)
    app.jobs["id"], app.leases["id"] = job, "one"
    await app.produce(job, {})
    assert not app.leases and job.finished.is_set()
    chunks = [job.queue.get_nowait() for _ in range(job.queue.qsize())]
    assert chunks[-1] == b'data: [DONE]\n\n'


async def test_truncated_stream_quarantines(tmp_path):
    app = Gateway(StreamingBackend(terminal=False), key="x" * 32, state_file=tmp_path / "state.json")
    job = Job("id", "one", True)
    app.jobs["id"], app.leases["id"] = job, "one"
    await app.produce(job, {})
    assert app.leases and not app.ready
    assert job.failure.code == "worker_stream_incomplete"


async def test_slow_reader_bounded_queue_and_drains(tmp_path):
    app = Gateway(StreamingBackend(count=100), key="x" * 32, state_file=tmp_path / "state.json")
    job = Job("id", "one", True)
    app.jobs["id"], app.leases["id"] = job, "one"
    await asyncio.wait_for(app.produce(job, {}), 1)
    assert job.queue.qsize() == 16
    assert job.detached and job.failure.code == "slow_consumer"
    assert not app.leases  # Terminal was observed while downstream stopped reading.
