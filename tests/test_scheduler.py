import asyncio
import base64
import concurrent.futures
import io
import json
import os
import sqlite3
import wave
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from selfhost_models.api import Gateway
from selfhost_models.scheduler_controller import Controller
from selfhost_models.scheduler_schema import Deployment, LoadConfig, SchedulerConfig, SchedulerError, SubmitJob
from selfhost_models.scheduler_store import Store


class Clock:
    now = 1000

    def __call__(self):
        return self.now


def wav(seconds=1, sample=0):
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        f.writeframes(sample.to_bytes(2, "little", signed=True) * int(seconds * 16000))
    return out.getvalue()


def setup_store(tmp_path, *, capacity=1, **config):
    clock = Clock()
    s = Store(tmp_path / "state", SchedulerConfig(**config), clock=clock)
    asset = tmp_path / "asset"
    asset.mkdir()
    (asset / "config.json").write_text('{}')
    (asset / "model.safetensors").write_bytes(b"synthetic-weights")
    ref = s.asset_register(asset)
    q = Deployment(model="Qwen/Qwen3.5-4B", revision="851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a", asset_ref=ref,
        runtime="vllm", runtime_version="vllm-0.29.0", image="sha256:" + "a" * 64, load=LoadConfig(capacity=capacity))
    w = Deployment(model="openai/whisper-small", revision="973afd24965f72e36ca33b3055d56a652f456b4d", asset_ref=ref,
        runtime="whisper", runtime_version="transformers-5.16.1", image="sha256:" + "b" * 64,
        load=LoadConfig(dtype="float16", attention="sdpa", capacity=1, host_memory_gib=8, shm_gib=1, gpu_memory=0.17))
    s.register(q)
    s.register(w)
    return s, q, w, clock


def submit(s, dep, key, **kwargs):
    value = {"deployment_id": dep.id, "operation": "chat" if dep.runtime == "vllm" else "transcribe",
             "input": {"model": dep.model, "messages": [{"role": "user", "content": "synthetic"}]}}
    if dep.runtime == "whisper":
        value["input"] = {"audio_ref": s.upload(wav())["artifact_ref"]}
    return s.submit(SubmitJob.model_validate({**value, **kwargs}), key)[0]


def test_pid_default_does_not_rewrite_registered_resource_contract(tmp_path):
    store, default, _, _ = setup_store(tmp_path)
    assert default.load.pids == 256
    explicit = default.model_copy(update={"load": default.load.model_copy(update={"pids": 128})})
    old_id = store.register(explicit)
    assert old_id != default.id

    reopened = Store(store.root)
    assert reopened.deployment(old_id).model_dump() == explicit.model_dump()
    assert reopened.deployment(default.id).load.pids == 256


class FakeProvider:
    def __init__(self):
        self.epoch = "worker-one"
        self.calls = []
        self.finished = {}
        self.alive = False
        self.load_gate = None
        self.warm_gate = None
        self.fail = None

    async def preflight(self):
        pass

    async def prepare(self, dep, path):
        pass

    def handle(self, dep, epoch):
        return "fixture-" + str(epoch)

    async def load(self, dep, path, handle):
        self.calls.append(("load", dep.id))
        self.alive = True
        if self.load_gate:
            await self.load_gate.wait()
        if self.fail == "load":
            raise RuntimeError("synthetic OOM")
        return self.epoch

    async def warmup(self, dep, epoch):
        self.calls.append(("warmup", dep.id))
        if self.warm_gate:
            await self.warm_gate.wait()
        if self.fail == "warmup":
            raise RuntimeError("synthetic warmup error")

    async def identity(self):
        return self.epoch

    async def execute(self, dep, spec, attempt, store):
        self.calls.append(("execute", attempt["job"]))
        event = self.finished.setdefault(attempt["job"], asyncio.Event())
        await event.wait()
        if self.fail == "execute":
            raise RuntimeError("synthetic lost response")
        return {"text": "synthetic result"}, None

    async def unload(self, state):
        self.calls.append(("unload", state["deployment"]))
        if self.fail == "unload":
            raise RuntimeError("synthetic stop error")
        self.alive = False

    async def exited(self, state):
        return not self.alive

    async def close(self):
        pass


async def tick(c, n=1):
    for _ in range(n):
        await c.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


async def finish(c, p, job):
    p.finished[job["id"]].set()
    await asyncio.gather(*list(c.tasks.values()))


def ready(s, dep):
    epoch = s.acquire_controller()
    s.phase(epoch, "loading", deployment=dep.id, handle="fixture")
    s.phase(epoch, "ready", worker_epoch="worker-one")
    s.heartbeat(epoch)
    return epoch


async def test_normal_132_and_exact_deployment(tmp_path):
    s, q, w, _ = setup_store(tmp_path)
    first, second, third = submit(s,q,"1"), submit(s,w,"2"), submit(s,q,"3")
    p, c = FakeProvider(), None
    c = Controller(s, p)
    await c.start()
    try:
        await tick(c, 2)
        await finish(c,p,first)
        await tick(c)
        await finish(c,p,third)
        await tick(c,2)
        await finish(c,p,second)
        assert [v for k,v in p.calls if k == "execute"] == [first["id"],third["id"],second["id"]]
        assert all(s.job(j["id"])["result_state"] == "available" for j in (first,second,third))
        modified = q.model_copy(update={"load": q.load.model_copy(update={"context": 4096})})
        changed_revision = q.model_copy(update={"revision": "c"*40})
        changed_runtime = q.model_copy(update={"runtime_version": "fixture-other"})
        assert len({q.id, modified.id, changed_revision.id, changed_runtime.id, w.id}) == 5
    finally:
        await c.close()


@pytest.mark.parametrize("boundary", ["queued", "loading", "warming", "running", "draining"])
async def test_urgent_boundaries_fifo_no_interrupt(tmp_path, boundary):
    s,q,w,_ = setup_store(tmp_path)
    normal = submit(s,q,"normal")
    p = FakeProvider()
    c = Controller(s,p)
    await c.start()
    loading = None
    try:
        if boundary in ("loading", "warming"):
            gate = asyncio.Event()
            if boundary == "loading": p.load_gate = gate
            else: p.warm_gate = gate
            loading = asyncio.create_task(c.tick())
            for _ in range(100):
                if s.state()["phase"] == boundary: break
                await asyncio.sleep(.001)
        elif boundary in ("running", "draining"):
            await tick(c,2)
            if boundary == "draining":
                submit(s,w,"switch",urgent=True)
                await tick(c)
                assert s.state()["phase"] == "draining"
        urgent1 = submit(s,w,"urgent-1",urgent=True)
        urgent2 = submit(s,q,"urgent-2",urgent=True)
        if loading:
            gate.set()
            await loading
        if boundary in ("running", "draining"):
            assert s.lease_count() == 1
            assert not any(k=="unload" for k,v in p.calls)
            await finish(c,p,normal)
        seen = []
        for _ in range(20):
            await tick(c)
            running = [(k,v) for k,v in p.calls if k=="execute" and v not in seen and not (v==normal["id"] and boundary in ("running","draining"))]
            for _, jid in running:
                seen.append(jid)
                await finish(c,p,{"id":jid})
            if urgent2["id"] in seen: break
        assert seen.index(urgent1["id"]) < seen.index(urgent2["id"])
        if boundary not in ("running", "draining"):
            assert normal["id"] not in seen
    finally:
        await c.close()


def test_normal_reuse_aging_and_continuous_urgent(tmp_path):
    s,q,w,clock = setup_store(tmp_path,reuse_dispatches=2,aging_seconds=20)
    epoch=ready(s,q)
    old=submit(s,w,"old")
    for i in range(2):
        current=submit(s,q,str(i))
        a=s.dispatch(epoch,current["id"])
        assert a
        s.terminal(epoch,a["id"],{"ok":True})
    submit(s,q,"third")
    assert s.next_job(epoch)["id"] == old["id"]
    for i in range(6):
        clock.now += 21
        urgent=submit(s,q,"urgent"+str(i),urgent=True)
        assert s.next_job(epoch)["id"] == urgent["id"]
        a=s.dispatch(epoch,urgent["id"])
        s.terminal(epoch,a["id"],{"ok":True})
    assert s.next_job(epoch)["id"] == old["id"]


def test_capacity_two_legacy_and_urgent_fencing(tmp_path):
    s,q,w,_=setup_store(tmp_path,capacity=2)
    epoch=ready(s,q)
    s.legacy_admit("legacy",q.id,"worker-one","api-one")
    urgent=submit(s,q,"urgent",urgent=True)
    assert s.dispatch(epoch,urgent["id"])
    assert s.lease_count()==2
    with pytest.raises(SchedulerError,match="overloaded"):
        s.legacy_admit("overflow",q.id,"worker-one","api-one")
    s.engine_exited(epoch)
    s.phase(epoch,"loading",deployment=w.id)
    s.phase(epoch,"ready",worker_epoch="worker-two")
    s.legacy_terminal("legacy","worker-one","api-one",False)
    s.legacy_terminal("legacy","worker-one","api-one",True)
    assert s.state()["phase"]=="ready" and s.state()["worker_epoch"]=="worker-two"


def test_dependencies_deadline_cancel_and_capacity(tmp_path):
    s,q,w,clock=setup_store(tmp_path,queue_capacity=4)
    epoch=ready(s,q)
    parent=submit(s,q,"p")
    child=submit(s,q,"c",depends_on=[parent["id"]])
    expired=submit(s,q,"exp",queue_timeout_seconds=1)
    canceled=submit(s,q,"cancel")
    with pytest.raises(SchedulerError,match="queue_capacity"): submit(s,q,"full")
    s.cancel(canceled["id"])
    clock.now+=2
    a=s.dispatch(epoch,parent["id"])
    assert s.next_job(epoch) is None
    s.terminal(epoch,a["id"],{"ok":True})
    assert s.next_job(epoch)["id"]==child["id"]
    assert s.job(expired["id"])["state"]=="expired"
    parent2=submit(s,q,"p2")
    child2=submit(s,q,"c2",depends_on=[parent2["id"]])
    s.cancel(parent2["id"])
    s.next_job(epoch)
    assert s.job(child2["id"])["state"]=="dependency_failed"


def test_concurrent_idempotency_response_loss_and_conflict(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    def call(_):
        other=Store(s.root)
        return submit(other,q,"one")["id"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        ids=list(pool.map(call,range(16)))
    assert len(set(ids))==1 and len(s.jobs())==1
    assert submit(s,q,"one")["id"]==ids[0]
    with pytest.raises(SchedulerError,match="idempotency_conflict"): submit(s,q,"one",urgent=True)


@pytest.mark.parametrize("fault", ["after_receipt","before_commit","publish","publish_commit"])
def test_terminal_and_storage_crashes_recover_without_compute(tmp_path,fault):
    s,q,_,_=setup_store(tmp_path)
    epoch=ready(s,q)
    j=submit(s,q,"one")
    a=s.dispatch(epoch,j["id"])
    fired=False
    def fail(point):
        nonlocal fired
        if point==fault and not fired:
            fired=True
            raise sqlite3.OperationalError("synthetic commit/disk failure")
    s.fault=fail
    with pytest.raises(sqlite3.Error): s.terminal(epoch,a["id"],{"unique_result":"retained"})
    assert s.receipt_path(a["id"]).exists()
    s.fault=lambda _:None
    other=Store(s.root,clock=s.clock)
    new_epoch=other.acquire_controller()
    other.recover_receipts(new_epoch)
    assert json.loads(other.result(j["id"]))=={"unique_result":"retained"}
    assert other.job(j["id"])["attempt"]==a["id"]
    assert other.lease_count()==0
    with pytest.raises(SchedulerError,match="stale_controller"): s.terminal(epoch,a["id"],{"bad":True})


def test_unknown_not_replayed_after_restart_or_engine_exit(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    epoch=ready(s,q)
    j=submit(s,q,"one")
    a=s.dispatch(epoch,j["id"])
    other=Store(s.root,clock=s.clock)
    next_epoch=other.acquire_controller()
    assert other.job(j["id"])["state"]=="unknown"
    assert other.lease_count()==1 and other.next_job(next_epoch) is None
    other.engine_exited(next_epoch)
    assert other.next_job(next_epoch) is None and other.job(j["id"])["attempt"]==a["id"]


def test_orphan_bytes_bounded_and_gc_commit_safe(tmp_path):
    s,q,_,clock=setup_store(tmp_path,storage_bytes=4194304,retention_seconds=60)
    def fail(_): raise OSError("synthetic commit failure")
    s.fault=fail
    for i in range(4):
        with pytest.raises(OSError): s.upload(wav(30,i))
    with pytest.raises(SchedulerError,match="storage_capacity"): s.upload(wav(30,5))
    s.fault=lambda _:None
    ref=s.upload(wav())["artifact_ref"]
    clock.now+=61
    s.fault=fail
    with pytest.raises(OSError): s.collect()
    assert s.artifact(ref)[0]==wav()
    s.fault=lambda point: (_ for _ in ()).throw(OSError("crash")) if point=="after_gc_commit" else None
    with pytest.raises(OSError): s.collect()
    assert s.artifact_path(ref).exists()
    s.fault=lambda _:None
    # Reupload same content between GC commit and unlink must remain readable.
    s.upload(wav())
    s.sweep()
    assert s.artifact(ref)[0]==wav()


def test_asset_full_hash_pins_and_manifest(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    epoch=ready(s,q)
    with pytest.raises(SchedulerError,match="asset_pinned"): s.asset_forget(q.asset_ref)
    path=tmp_path/"asset"/"model.safetensors"
    stamp=path.stat()
    path.write_bytes(b"SYNTHETIC-WEIGHTS")
    os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    with pytest.raises(SchedulerError,match="asset_hash_mismatch"): s.asset_verify(q.asset_ref)
    path.unlink()
    with pytest.raises(SchedulerError): s.asset_verify(q.asset_ref)


@pytest.mark.parametrize("field", ["urgent","input","path","command","url"])
def test_typed_schema_and_fixed_whisper(tmp_path,field):
    s,q,w,_=setup_store(tmp_path)
    base={"deployment_id":q.id,"operation":"chat","input":{"model":q.model,"messages":[{"role":"user","content":"x"}]}}
    bad={**base,field:"unexpected"}
    with pytest.raises(ValidationError): SubmitJob.model_validate(bad)
    bad=w.model_dump();bad["revision"]="d"*40
    with pytest.raises(ValidationError): Deployment.model_validate(bad)


async def test_resources_remain_available_during_switch_and_auth(tmp_path):
    s,q,w,_=setup_store(tmp_path)
    g=Gateway(key="k"*32,scheduler=s,state_file=tmp_path/"ignored.json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(g),base_url="http://test",headers={"Authorization":"Bearer "+"k"*32}) as client:
        assert (await client.get("/health/ready")).status_code==503
        assert (await client.get("/v1/catalog")).status_code==200
        assert (await client.get("/v1/catalog",headers={"Authorization":"Bearer wrong"})).status_code==401
        spec={"deployment_id":q.id,"operation":"chat","input":{"model":q.model,"messages":[{"role":"user","content":"secret synthetic"}]},"urgent":True}
        response=await client.post("/v1/jobs",json=spec,headers={"Idempotency-Key":"client-one"})
        assert response.status_code==201
        jid=response.json()["id"]
        assert "secret synthetic" not in response.text
        assert (await client.post("/v1/jobs",json=spec,headers={"Idempotency-Key":"client-one"})).status_code==200
        assert (await client.get(f"/v1/jobs/{jid}/result")).status_code==409
        assert (await client.post(f"/v1/jobs/{jid}/cancel",json={})).json()["state"]=="canceled"
        for extra in ({"shell":"x"},{"path":"/etc/passwd"}):
            assert (await client.post("/v1/jobs",json={**spec,**extra},headers={"Idempotency-Key":"bad"})).status_code==400


@pytest.mark.parametrize("failure", ["load","warmup","execute","unload"])
async def test_lifecycle_failures_keep_resource_until_confirmed_exit(tmp_path,failure):
    s,q,w,_=setup_store(tmp_path)
    p=FakeProvider();c=Controller(s,p)
    await c.start()
    try:
        j=submit(s,q,"one")
        if failure in ("load","warmup"):
            p.fail=failure
            await tick(c)
        else:
            await tick(c,2)
            p.fail=failure
            await finish(c,p,j)
            if failure=="unload":
                submit(s,w,"two",urgent=True)
                await tick(c)
        assert s.state()["phase"]=="unknown"
        assert p.alive
        await tick(c)
        assert s.state()["phase"]=="unknown"
        p.alive=False
        await tick(c)
        assert s.state()["phase"]=="unloaded"
    finally:
        await c.close()


async def test_api_served_models_qwen_whisper_and_authoritative_health(tmp_path,monkeypatch):
    from selfhost_models import scheduler_runtime
    s,q,w,_=setup_store(tmp_path,capacity=2)
    epoch=ready(s,q)
    (s.root/"worker-key").write_text("internal"*8)
    class Backend:
        async def identity(self): return s.state()["worker_epoch"]
        async def close(self): pass
    class Provider:
        def __init__(self,store): self.secret_path=store.root/"worker-key"
        def set_backend(self,dep): self.backend=Backend()
    monkeypatch.setattr(scheduler_runtime,"DockerProvider",Provider)
    g=Gateway(key="public"*8,scheduler=s)
    monitor=asyncio.create_task(g.watch_scheduler())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(g),base_url="http://test",headers={"Authorization":"Bearer "+"public"*8}) as client:
            for dep in (q,w,q):
                if s.state()["deployment"]!=dep.id:
                    s.engine_exited(epoch)
                    s.phase(epoch,"loading",deployment=dep.id)
                    s.phase(epoch,"ready",worker_epoch=dep.id[-12:])
                    assert (await client.get("/v1/models")).status_code==503  # no stale model while watcher catches up
                await asyncio.sleep(.12)
                response=await client.get("/v1/models")
                assert response.status_code==200 and response.json()["data"][0]["id"]==dep.model
                assert response.json()["data"][0]["backend"]==dep.runtime
                if dep.runtime=="whisper":
                    assert response.json()["data"][0]["capabilities"]["transcribe"]
                    assert (await client.post("/v1/chat/completions",json={"model":dep.model,"messages":[{"role":"user","content":"x"}]})).status_code==400
            worker=s.state()["worker_epoch"]
            s.legacy_admit("legacy",q.id,worker,"api-owner")
            j=submit(s,q,"async",urgent=True)
            s.dispatch(epoch,j["id"])
            s.legacy_detach("legacy","api-owner")
            health=(await client.get("/health/ready")).json()
            assert health["inflight"]==2 and health["detached"]==1
            s.legacy_restart("new-api")
            response=await client.get("/health/ready")
            assert response.status_code==503 and response.json()["uncertain"]==1
            assert (await client.get("/v1/jobs")).status_code==200
    finally:
        monitor.cancel()
        await asyncio.gather(monitor,return_exceptions=True)


def test_concurrent_upload_quota_and_pending_result_retention(tmp_path):
    s,q,_,clock=setup_store(tmp_path,storage_bytes=4194304,result_bytes=1024,retention_seconds=60)
    def put(i):
        try: return s.upload(wav(30,i))
        except SchedulerError as exc: return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(put,range(8)))
    assert sum(isinstance(r,dict) for r in results)==4
    assert results.count("storage_capacity")==4
    epoch=ready(s,q)
    j=submit(s,q,"pending")
    a=s.dispatch(epoch,j["id"])
    s.fault=lambda point: (_ for _ in ()).throw(OSError("disk")) if point=="publish" else None
    with pytest.raises(OSError): s.terminal(epoch,a["id"],{"sole":"result"})
    s.fault=lambda _:None
    clock.now+=61
    s.collect()
    assert s.receipt_path(a["id"]).exists() and s.job(j["id"])["result_state"]=="storage_failed"
    s.recover_receipts(epoch)
    assert json.loads(s.result(j["id"]))=={"sole":"result"}


async def test_load_hang_keeps_pin_and_cancel_while_loading_never_dispatches(tmp_path):
    s,q,_,_=setup_store(tmp_path,load_seconds=1)
    p=FakeProvider();p.load_gate=asyncio.Event()
    c=Controller(s,p);await c.start()
    try:
        j=submit(s,q,"one")
        task=asyncio.create_task(c.tick())
        for _ in range(100):
            if p.alive: break
            await asyncio.sleep(.001)
        s.cancel(j["id"])
        await task
        assert s.state()["phase"]=="unknown" and not any(k=="execute" for k,v in p.calls)
        with pytest.raises(SchedulerError,match="asset_pinned"): s.asset_forget(q.asset_ref)
    finally:
        await c.close()


async def test_cross_deployment_urgent_seals_free_slot(tmp_path):
    s,q,w,_=setup_store(tmp_path,capacity=2)
    p=FakeProvider();c=Controller(s,p);await c.start()
    try:
        first=submit(s,q,"one")
        await tick(c,2)
        urgent=submit(s,w,"urgent",urgent=True)
        submit(s,q,"normal")
        await tick(c)
        assert s.state()["phase"]=="draining" and s.lease_count()==1
        assert len([x for x in p.calls if x[0]=="execute"])==1
        await finish(c,p,first)
        await tick(c,2)
        assert [v for k,v in p.calls if k=="execute"][-1]==urgent["id"]
    finally:
        await c.close()


async def test_lifecycle_failure_blocks_deployment_without_automatic_retry(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    p=FakeProvider();p.fail="warmup";c=Controller(s,p)
    await c.start()
    try:
        j=submit(s,q,"one")
        await tick(c)
        assert s.job(j["id"])["state"]=="failed"
        p.alive=False
        await tick(c,5)
        assert len([v for k,v in p.calls if k=="load"])==1
        with pytest.raises(SchedulerError,match="deployment_blocked"): submit(s,q,"two")
        s.resume_deployment(q.id)
        p.fail=None
        submit(s,q,"two")
        await tick(c)
        assert len([v for k,v in p.calls if k=="load"])==2
    finally:
        await c.close()


async def test_persisted_budgets_include_warmup_and_legacy_and_stop_loads(tmp_path):
    s,q,w,_=setup_store(tmp_path,max_load_attempts=1,max_generation_attempts=3)
    p=FakeProvider();c=Controller(s,p);await c.start()
    try:
        j=submit(s,q,"one")
        await tick(c,2)  # Qwen cap1: two warmups + one job = three reservations.
        await finish(c,p,j)
        assert s.budget_state()=={"loads":1,"generations":3}
        with pytest.raises(SchedulerError,match="generation_budget_exhausted"):
            s.legacy_admit("extra",q.id,"worker-one","api")
        extra=submit(s,w,"second")
        await tick(c,3)
        assert s.job(extra["id"])["state"]=="failed"
        assert len([v for k,v in p.calls if k=="load"])==1
        assert Store(s.root).budget_state()==s.budget_state()
    finally:
        await c.close()


async def test_managed_legacy_unknown_does_not_poison_local_capacity(tmp_path):
    from test_gateway import FakeBackend
    s,q,_,_=setup_store(tmp_path)
    epoch=ready(s,q)
    backend=FakeBackend();backend.epoch="worker-one";backend.finish.set();backend.fail=True
    g=Gateway(backend,key="k"*32,scheduler=s)
    g.model=q.model;g.epoch="worker-one";g.ready=True;g.capacity=1;g.deployment_id=q.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(g),base_url="http://test",headers={"Authorization":"Bearer "+"k"*32}) as client:
        payload={"model":q.model,"messages":[{"role":"user","content":"synthetic"}]}
        assert (await client.post("/v1/chat/completions",json=payload)).status_code==502
        assert s.lease_count()==1
        s.engine_exited(epoch)
        s.phase(epoch,"loading",deployment=q.id)
        s.phase(epoch,"ready",worker_epoch="worker-two")
        backend.epoch="worker-two";backend.fail=False;g.epoch="worker-two";g.ready=True
        assert (await client.post("/v1/chat/completions",json=payload)).status_code==200
        assert s.lease_count()==0


async def test_per_job_result_description_survives_shared_bytes_and_restart(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    epoch=ready(s,q)
    first=submit(s,q,"description-one",result_description="第一份成果")
    second=submit(s,q,"description-two",result_description="另一份成果")
    for job in (first,second):
        attempt=s.dispatch(epoch,job["id"])
        s.terminal(epoch,attempt["id"],{"text":"identical result bytes"})
        s.publish(epoch,attempt["id"])
    s=Store(s.root)
    assert s.job(first["id"])["result_ref"]==s.job(second["id"])["result_ref"]
    g=Gateway(key="k"*32,scheduler=s)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(g),base_url="http://test",headers={"Authorization":"Bearer "+"k"*32}) as client:
        for job,description in ((first,"第一份成果"),(second,"另一份成果")):
            response=await client.get("/v1/jobs/"+job["id"])
            assert response.json()["result_description"]==description
            assert response.json()["result_state"]=="available"
            assert (await client.get("/v1/jobs/"+job["id"]+"/result")).json()=={"text":"identical result bytes"}
        listing=(await client.get("/v1/jobs")).json()["data"]
        assert [x["result_description"] for x in listing]==["第一份成果","另一份成果"]


@pytest.mark.parametrize("failure",["image","network","mount"])
async def test_docker_preparation_failure_has_no_external_start_and_is_recoverable(tmp_path,failure):
    from selfhost_models.scheduler_runtime import DockerProvider
    s,q,_,_=setup_store(tmp_path)
    class PreparationFixture(DockerProvider):
        async def preflight(self):
            pass
        async def command(self,*args,**kwargs):
            calls.append(args)
            if args[:2]==("image","inspect"):
                if failure=="image": raise SchedulerError("provider_command_failed",503)
                return b"[{}]"
            if args[:2] in (("network","inspect"),("network","create")):
                raise SchedulerError("provider_command_failed",503)
            raise AssertionError("unexpected external start/inspect: "+repr(args))
    calls=[];p=PreparationFixture(s)
    if failure=="mount":p.secret_path=s.root/"invalid,mount"
    c=Controller(s,p);await c.start()
    try:
        j=submit(s,q,"preparation")
        await tick(c,5)
        assert s.state()["phase"]=="unloaded" and s.state()["handle"] is None
        assert s.job(j["id"])["state"]=="failed" and s.job(j["id"])["attempt"] is None
        assert s.lease_count()==0 and s.budget_state()["loads"]==1
        assert not any(args[0] in ("run","start","create") for args in calls)
        with pytest.raises(SchedulerError,match="deployment_blocked"):submit(s,q,"blocked")
        s.resume_deployment(q.id)
        again=submit(s,q,"operator-retry")
        await tick(c,3)
        assert s.job(again["id"])["state"]=="failed" and s.budget_state()["loads"]==2
    finally:
        await c.close()


def test_crash_during_preparation_is_distinct_from_external_start_intent(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    epoch=s.acquire_controller();j=submit(s,q,"preparing")
    s.phase(epoch,"loading",deployment=q.id)
    s.reserve_load(epoch,q)
    epoch=s.acquire_controller()
    assert s.state()["phase"]=="unloaded" and s.job(j["id"])["error"]=="preparation_interrupted"
    assert s.budget_state()["loads"]==1
    s.resume_deployment(q.id)
    submit(s,q,"external-intent")
    s.phase(epoch,"loading",deployment=q.id,handle="external-might-have-started")
    s.acquire_controller()
    assert s.state()["phase"]=="unknown"
    with pytest.raises(SchedulerError,match="engine_exit_unconfirmed"):s.resume_deployment(q.id)


def test_budget_window_load_reserves_full_warmup_atomically_and_survives_restart(tmp_path):
    s,q,w,_=setup_store(tmp_path,capacity=2)
    s.open_budget_window("handoff",1,3)
    epoch=s.acquire_controller()
    # Capacity-two Qwen requires four warmups before any job can run.
    with pytest.raises(SchedulerError,match="budget_window_exhausted"):
        s.reserve_load(epoch,q)
    assert s.budget_state()=={"loads":0,"generations":0}
    s.reserve_load(epoch,w)
    reopened=Store(s.root)
    with pytest.raises(SchedulerError,match="budget_window_exhausted"):
        reopened.reserve_load(epoch,w)
    assert reopened.budget_state()=={"loads":1,"generations":1}
    assert reopened.budget_windows()[0]["used_loads"]==1


def test_budget_window_durable_and_legacy_share_atomic_admission(tmp_path):
    s,q,_,_=setup_store(tmp_path,capacity=2)
    s.open_budget_window("handoff",2,1)
    epoch=ready(s,q)
    first=submit(s,q,"first")
    attempt=s.dispatch(epoch,first["id"])
    with pytest.raises(SchedulerError,match="budget_window_requires_idle"):
        s.close_budget_window("handoff")
    with pytest.raises(SchedulerError,match="budget_window_exhausted"):
        s.legacy_admit("legacy",q.id,"worker-one","api")
    second=submit(s,q,"second")
    assert s.dispatch(epoch,second["id"]) is None
    assert s.job(second["id"])["error"]=="budget_window_exhausted"
    assert s.job(second["id"])["attempt"] is None
    s.terminal(epoch,attempt["id"],{"text":"synthetic"})
    s.publish(epoch,attempt["id"])
    s.close_budget_window("handoff")
    closed=s.budget_windows()[0]
    s.legacy_admit("after",q.id,"worker-one","api")
    s.legacy_terminal("after","worker-one","api",True)
    s.close_budget_window("handoff")
    reopened=Store(s.root)
    assert reopened.budget_state()=={"loads":0,"generations":2}
    assert reopened.budget_windows()==[closed]
    assert closed["used_generations"]==1 and closed["end_generations"]==1
    kinds=[e["kind"] for e in reopened.events()]
    assert kinds.count("budget_window_opened")==1 and kinds.count("budget_window_closed")==1
    with pytest.raises(SchedulerError,match="budget_window_exists"):
        s.open_budget_window("handoff",2,2)


def test_budget_window_concurrent_legacy_never_overspends(tmp_path):
    s,q,_,_=setup_store(tmp_path,capacity=2)
    s.open_budget_window("concurrent",1,1)
    ready(s,q)
    def admit(i):
        try:
            s.legacy_admit(str(i),q.id,"worker-one","api")
            return "admitted"
        except SchedulerError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(admit,range(2)))
    assert sorted(results)==["admitted","budget_window_exhausted"]
    assert s.budget_state()["generations"]==1 and s.lease_count()==1


@pytest.mark.parametrize("phase",["loading","warming","draining","unloading","unknown"])
def test_budget_window_cannot_close_across_lifecycle_boundary(tmp_path,phase):
    s,q,_,_=setup_store(tmp_path)
    s.open_budget_window("handoff",1,1)
    epoch=s.acquire_controller()
    s.phase(epoch,phase,deployment=q.id)
    with pytest.raises(SchedulerError,match="budget_window_requires_idle"):
        s.close_budget_window("handoff")
    assert s.budget_windows()[0]["closed"] is None


def test_budget_window_unknown_blocks_close_until_verified_engine_exit(tmp_path):
    s,q,_,_=setup_store(tmp_path)
    s.open_budget_window("handoff",1,1)
    epoch=ready(s,q)
    job=submit(s,q,"one")
    with pytest.raises(SchedulerError,match="budget_window_requires_idle"):
        s.close_budget_window("handoff")
    attempt=s.dispatch(epoch,job["id"])
    s.attempt_state(epoch,attempt["id"],"unknown")
    with pytest.raises(SchedulerError,match="budget_window_requires_idle"):
        s.close_budget_window("handoff")
    s.engine_exited(epoch)
    assert s.lease_count()==0 and s.state()["phase"]=="unloaded"
    s.close_budget_window("handoff")
    assert s.job(job["id"])["state"]=="unknown"
    assert s.dispatch(epoch,job["id"]) is None
    assert s.budget_state()=={"loads":0,"generations":1}


def test_closing_temporary_window_never_bypasses_lifetime_budget(tmp_path):
    s,q,w,_=setup_store(tmp_path,max_load_attempts=1,max_generation_attempts=1)
    s.open_budget_window("handoff",2,2)
    epoch=s.acquire_controller()
    s.reserve_load(epoch,w)
    s.close_budget_window("handoff")
    with pytest.raises(SchedulerError,match="lifecycle_budget_exhausted"):
        s.reserve_load(epoch,w)
    assert s.budget_state()=={"loads":1,"generations":1}
