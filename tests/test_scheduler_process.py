import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest

from selfhost_models.scheduler_controller import Controller
from selfhost_models.scheduler_runtime import DockerProvider
from test_scheduler import setup_store, submit, tick


class ProcessProvider(DockerProvider):
    def __init__(self, store):
        super().__init__(store)
        self.process = None
        self.key = "synthetic-worker-key-" + "a" * 32
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.url = f"http://127.0.0.1:{s.getsockname()[1]}"

    async def preflight(self):
        pass

    async def prepare(self, dep, path):
        pass  # CPU fixture deliberately has no Docker/image/network preparation.

    async def load(self, dep, path, handle):
        self.process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).parent / "fixtures/scheduler_process.py"), "--port", self.url.rsplit(":",1)[1], "--key", self.key,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        assert (await asyncio.wait_for(self.process.stdout.readline(), 5)).strip() == b"ready"
        self.set_backend(dep)
        return await self.identity()

    async def warmup(self, dep, epoch):
        r=await self.backend.client.post("/internal/warmup",json={})
        assert r.headers["x-worker-epoch"]==epoch and r.json()["terminal"]

    async def exited(self, state):
        return self.process is not None and self.process.returncode is not None

    async def stop_fixture(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()


@pytest.mark.asyncio
async def test_real_subprocess_cancel_restart_no_release_until_actual_exit(tmp_path):
    s,q,w,_=setup_store(tmp_path)
    p=ProcessProvider(s)
    c=Controller(s,p)
    await c.start()
    j=submit(s,q,"one",urgent=True)
    try:
        await tick(c,2)
        async with httpx.AsyncClient(base_url=p.url,trust_env=False) as client:
            assert (await client.get("/health")).status_code==401
            assert (await client.get("/health",headers={"x-selfhost-worker-key":"public-key"})).status_code==401
            for _ in range(100):
                r=await client.get("/health",headers={"x-selfhost-worker-key":p.key})
                if r.json()["calls"]==1: break
                await asyncio.sleep(.01)
            assert r.json()["calls"]==1
        s.cancel(j["id"])
        assert s.lease_count()==1 and p.process.returncode is None
        await c.close()
        assert p.process.returncode is None and s.lease_count()==1
        # A new controller cannot infer exit from HTTP cancellation or its own restart.
        c=Controller(s,p)
        await c.start()
        await tick(c)
        assert s.state()["phase"]=="unknown" and s.job(j["id"])["state"]=="unknown"
        await p.stop_fixture()
        await tick(c)
        assert s.lease_count()==0 and s.state()["phase"]=="unloaded"
        assert s.next_job(c.epoch) is None
    finally:
        await c.close()
        await p.stop_fixture()


@pytest.mark.asyncio
async def test_real_subprocess_terminal_storage_retry_never_regenerates(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    p=ProcessProvider(s);c=Controller(s,p)
    await c.start()
    j=submit(s,q,"one",urgent=True)
    try:
        await tick(c,2)
        for _ in range(100):
            if (await p.backend.client.get("/health")).json()["calls"]==1: break
            await asyncio.sleep(.01)
        def fail(point):
            if point=="publish": raise OSError("synthetic storage unavailable")
        s.fault=fail
        await p.backend.client.post("/internal/complete",json={})
        await asyncio.gather(*list(c.tasks.values()))
        assert s.job(j["id"])["result_state"]=="storage_failed" and s.lease_count()==0
        s.fault=lambda _:None
        await tick(c)
        assert s.job(j["id"])["result_state"]=="available"
        assert (await p.backend.client.get("/health")).json()["calls"]==1
    finally:
        await c.close()
        await p.stop_fixture()
