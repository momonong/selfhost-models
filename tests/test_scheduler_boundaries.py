import asyncio
import base64
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from selfhost_models.gpu_ownership import GPUOwnership
from selfhost_models.scheduler_schema import SchedulerError
from selfhost_models import scheduler_cli
from worker.identity import WorkerIdentity
from worker.relay import Relay
from worker.whisper_app import WhisperWorker
from worker.whisper_engine import validate_identity
from test_scheduler import wav, setup_store


def test_daemon_ownership_two_states_modes_no_heartbeat_steal(tmp_path):
    volumes={};guard=threading.Lock()
    def run(args):
        with guard:
            if args[:2]==["volume","create"]:
                labels=dict(args[i+1].split("=",1) for i,x in enumerate(args) if x=="--label")
                volumes.setdefault(args[-1],{"Labels":labels})
                return args[-1]
            if args[:2]==["volume","inspect"]: return json.dumps([volumes[args[-1]]])
            if args==["ps","-q"]: return ""
            if args[:2]==["volume","rm"]: return volumes.pop(args[-1])
            raise AssertionError(args)
    a=GPUOwnership(tmp_path/"windows","static",run=run)
    b=GPUOwnership(tmp_path/"linux","managed",run=run)
    a.acquire()
    with pytest.raises(SchedulerError,match="gpu_owner_conflict"): b.acquire()
    with pytest.raises(SchedulerError,match="gpu_owner_conflict"): b.release()
    a.acquire()  # same owner may restart, never a different state/mode
    a.release();b.acquire()
    with pytest.raises(SchedulerError,match="gpu_owner_conflict"): a.release()


async def test_worker_secret_separate_allowlist_no_public_header_override(tmp_path,monkeypatch):
    key=tmp_path/"worker-key";key.write_text("internal"*8)
    monkeypatch.setenv("WORKER_API_KEY_FILE",str(key))
    seen=[]
    async def upstream(scope,receive,send):
        seen.append(scope["path"])
        await send({"type":"http.response.start","status":200,"headers":[]})
        await send({"type":"http.response.body","body":b"{}"})
    app=WorkerIdentity(upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as c:
        for headers in ({},{"Authorization":"Bearer "+"public"*8},{"x-selfhost-worker-key":"public"*8}):
            assert (await c.post("/v1/chat/completions",json={},headers=headers)).status_code==401
        headers={"x-selfhost-worker-key":"internal"*8,"Authorization":"Bearer irrelevant"}
        assert (await c.post("/arbitrary/command",json={},headers=headers)).status_code==404
        r=await c.post("/v1/chat/completions",json={},headers=headers)
        assert r.status_code==200 and r.headers["x-worker-epoch"]
    assert seen==["/v1/chat/completions"]


async def test_relay_preserves_epoch_body_and_rejects_untrusted_routes():
    app=Relay("http://selfhost-scheduler-fixture:8000","internal"*8)
    await app.client.aclose()
    bodies=[]
    def transport(request):
        bodies.append(json.loads(request.content))
        assert request.headers["x-selfhost-worker-key"]=="internal"*8
        assert "authorization" not in request.headers
        return httpx.Response(200,json={"terminal":True},headers={"x-worker-epoch":"real-engine"})
    app.client=httpx.AsyncClient(transport=httpx.MockTransport(transport),base_url=app.target)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as c:
            assert (await c.post("/internal/transcribe",json={})).status_code==401
            headers={"x-selfhost-worker-key":"internal"*8}
            assert (await c.post("/../../command",json={},headers=headers)).status_code==404
            r=await c.post("/internal/transcribe",json={"synthetic":"input"},headers=headers)
            assert r.status_code==200 and r.headers["x-worker-epoch"]=="real-engine" and r.json()["terminal"]
            assert bodies==[{"synthetic":"input"}]
    finally:
        await app.client.aclose()


@pytest.mark.parametrize("runtime", ["vllm", "whisper"])
async def test_provider_relay_uses_python3_in_fixed_worker_image(tmp_path, runtime):
    from selfhost_models.scheduler_runtime import DockerProvider
    store, qwen, whisper, _ = setup_store(tmp_path)
    deployment = qwen if runtime == "vllm" else whisper
    calls = []

    class RelayCommands(DockerProvider):
        async def command(self, *args, **kwargs):
            calls.append(args)
            if args[:2] == ("network", "inspect"):
                return json.dumps([{"Labels": {"selfhost.scheduler": self.namespace}}]).encode()
            if args[0] == "create":
                self.created = args
                return b"synthetic-relay-id"
            if args[:2] == ("network", "connect"):
                return b""
            if args[0] == "start":
                # The fixed vLLM image exposes python3 without a python alias.
                available = {"python3"} if runtime == "vllm" else {"python", "python3"}
                executable = self.created[self.created.index("--entrypoint") + 1]
                if executable not in available:
                    raise SchedulerError("provider_command_failed", 503)
                return b"synthetic-relay-id"
            raise AssertionError(args)

    provider = RelayCommands(store)
    await provider.start_relay(deployment, "synthetic-worker")
    created = next(args for args in calls if args[0] == "create")
    assert created[-5:] == ("--entrypoint", "python3", deployment.image, "-m", "worker.relay")
    assert "--pull=never" in created and "--gpus" not in created
    assert calls[-2:] == [
        ("network", "connect", provider.network, "synthetic-worker-relay"),
        ("start", "synthetic-worker-relay"),
    ]


@pytest.mark.parametrize("runtime", ["vllm", "whisper"])
async def test_provider_readonly_worker_redirects_caches_to_bounded_tmpfs(tmp_path, runtime):
    from selfhost_models.scheduler_runtime import DockerProvider
    store, qwen, whisper, _ = setup_store(tmp_path)
    deployment = qwen if runtime == "vllm" else whisper
    calls = []

    class LoadCommands(DockerProvider):
        async def command(self, *args, **kwargs):
            calls.append(args)
            assert args[0] == "run"
            return b"synthetic-worker-id"

        async def start_relay(self, dep, handle):
            pass

        def set_backend(self, dep):
            pass

        async def identity(self):
            return "synthetic-worker-epoch"

    provider = LoadCommands(store)
    assert await provider.load(deployment, tmp_path / "asset", "synthetic-worker") == "synthetic-worker-epoch"
    args, = calls
    env = dict(args[i + 1].split("=", 1) for i, value in enumerate(args) if value == "-e")
    expected = {
        "XDG_CACHE_HOME": "/runtime-cache/cache",
        "XDG_CONFIG_HOME": "/runtime-cache/config",
        "HF_HOME": "/runtime-cache/cache/huggingface",
        "VLLM_CACHE_ROOT": "/runtime-cache/cache/vllm",
        "VLLM_CONFIG_ROOT": "/runtime-cache/config/vllm",
        "TORCHINDUCTOR_CACHE_DIR": "/runtime-cache/cache/torchinductor",
        "TRITON_CACHE_DIR": "/runtime-cache/cache/triton",
    }
    assert {key: env[key] for key in expected} == expected
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    mounts = [args[i + 1] for i, value in enumerate(args) if value == "--tmpfs"]
    assert mounts == [
        "/tmp:rw,noexec,nosuid,nodev,size=128m",
        "/runtime-cache:rw,nosuid,nodev,size=1g,uid=10001,gid=10001",
    ]
    assert "--read-only" in args and "--pull=never" in args
    assert "TMPDIR" not in env  # No unverified alternate temporary-directory contract.


class ASREngine:
    def __init__(self):
        self.entered=threading.Event();self.finish=threading.Event();self.fail=False

    def run(self,*args):
        self.entered.set();self.finish.wait(5)
        if self.fail:raise RuntimeError("synthetic CUDA/OOM stand-in")
        return {"terminal":True,"text":"synthetic","truncated":True,"finish_reason":"length"}


async def test_asr_worker_cancellation_retains_active_and_truncation():
    engine=ASREngine();app=WhisperWorker(engine)
    body={"model":"openai/whisper-small","audio_base64":base64.b64encode(wav()).decode(),"max_tokens":1}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url="http://test") as c:
        call=asyncio.create_task(c.post("/internal/transcribe",json=body))
        for _ in range(100):
            if engine.entered.is_set():break
            await asyncio.sleep(.001)
        assert engine.entered.is_set()
        call.cancel();await asyncio.gather(call,return_exceptions=True)
        assert app.active is not None
        assert (await c.post("/internal/transcribe",json=body)).status_code==429
        task=app.active;engine.finish.set()
        status,result=await task
        assert status==200 and result["finish_reason"]=="length" and result["truncated"]
        for extra in ({"url":"https://example.com"},{"urgent":True},{"max_tokens":257}):
            assert (await c.post("/internal/transcribe",json={**body,**extra})).status_code==400
        engine.fail=True
        assert (await c.post("/internal/transcribe",json=body)).status_code==503
        assert (await c.get("/health")).status_code==503


def test_worker_revision_and_weight_hash_rejected(tmp_path):
    with pytest.raises(ValueError,match="identity"):
        validate_identity(tmp_path,"openai/whisper-small","0"*40)
    (tmp_path/"config.json").write_text(json.dumps({"model_type":"whisper","architectures":["WhisperForConditionalGeneration"],"d_model":768}))
    (tmp_path/"model.safetensors").write_bytes(b"wrong")
    with pytest.raises(ValueError,match="hash"):
        validate_identity(tmp_path,"openai/whisper-small","973afd24965f72e36ca33b3055d56a652f456b4d")


def test_windows_client_commands_never_open_sqlite(tmp_path,monkeypatch,capsys):
    key=tmp_path/"public-key";key.write_text("public"*8)
    monkeypatch.setattr(scheduler_cli,"Store",Mock(side_effect=AssertionError("client opened DB")))
    def response(request):
        assert request.headers["Authorization"]=="Bearer "+"public"*8
        return httpx.Response(200,json={"data":[]})
    client=httpx.Client(transport=httpx.MockTransport(response),base_url="http://127.0.0.1:18081",headers={"Authorization":"Bearer "+"public"*8})
    monkeypatch.setattr(scheduler_cli.httpx,"Client",lambda **_:client)
    args=SimpleNamespace(scheduler_command="catalog",state=tmp_path/"DO-NOT-CREATE",url="http://127.0.0.1:18081",api_key_file=key)
    assert scheduler_cli.main(args)==0
    assert not args.state.exists()
