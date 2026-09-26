"""CPU-only control/executor processes with a per-process Landlock allowlist.

The runtime deliberately has no Docker, model-library or GPU integration.
Only this fixture exposes diagnostics and a deterministic stream completion gate.
"""
import asyncio
import base64
import contextlib
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
import uuid

import httpx
import uvicorn

from selfhost_models.execution_client import RemoteBackend, RemoteProvider
from selfhost_models.executor import Executor, Journal
from selfhost_models.scheduler_controller import Controller
from selfhost_models.scheduler_schema import Deployment, LoadConfig, SchedulerError, SubmitJob
from selfhost_models.scheduler_store import Store


def isolate(root, forbidden):
    """Restrict this process only; no privilege, mount namespace or host changes."""
    if sys.platform != "linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise NotImplementedError("Landlock fixture requires Linux x86_64/aarch64")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 1:
        raise NotImplementedError(f"Landlock unavailable: errno={ctypes.get_errno()}")
    # REFER and TRUNCATE appeared in ABI 2 and 3 respectively.
    handled = (1 << (15 if abi >= 3 else 14 if abi >= 2 else 13)) - 1
    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]
    ruleset = Ruleset(handled)
    fd = libc.syscall(444, ctypes.byref(ruleset), ctypes.sizeof(ruleset), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    read = (1 << 0) | (1 << 2) | (1 << 3)
    paths = [(root, handled)]
    # Project source and Python runtimes are shared read-only code, never state.
    for path in (Path(__file__).resolve().parents[2], Path(sys.prefix), Path(sys.base_prefix),
                 Path("/usr"), Path("/lib"), Path("/lib64"), Path("/etc")):
        if path.exists():
            paths.append((path.resolve(), read))
    paths.append((Path("/dev/null"), (1 << 1) | (1 << 2)))
    try:
        for path, access in dict(paths).items():
            parent = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = PathRule(access, parent)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) < 0:
                    raise OSError(ctypes.get_errno(), "landlock_add_rule")
            finally:
                os.close(parent)
        if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
            raise OSError(ctypes.get_errno(), "no_new_privs")
        if libc.syscall(446, fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)
    denied = []
    for item in forbidden:
        try:
            if item["directory"]:
                list(Path(item["path"]).iterdir())
            else:
                Path(item["path"]).read_bytes()
        except PermissionError as exc:
            assert exc.errno in (errno.EACCES, errno.EPERM)
            denied.append({"label": item["label"], "error": "PermissionError"})
        else:
            raise AssertionError("cross-plane path unexpectedly readable: " + item["label"])
    return {"mechanism": "landlock", "abi": abi, "pid": os.getpid(), "denied": denied}


class CPURuntime:
    def __init__(self, root):
        self.secret_path = root / "unused-worker-key"
        self.backend = None
        self.alive = False
        self.epoch = None
        self.container_id = None
        self.calls = []
        self.stream_gates = {}

    async def prepare(self, dep, path):
        assert (path / "model.safetensors").read_bytes() == b"cpu-fixture-only"
        self.calls.append({"kind": "prepare", "model": dep.model})

    async def load(self, dep, path, handle):
        assert not self.alive
        self.alive = True
        self.epoch = uuid.uuid4().hex
        self.container_id = uuid.uuid4().hex
        self.calls.append({"kind": "load", "model": dep.model})
        return self.epoch

    def set_backend(self, dep):
        self.backend = self

    async def identity(self):
        assert self.alive
        return self.epoch

    async def warmup(self, dep, epoch):
        assert self.alive and epoch == self.epoch
        self.calls.append({"kind": "warmup", "model": dep.model})

    async def inspect(self, state):
        return {"Id": self.container_id}

    async def exited(self, state):
        return not self.alive

    async def unload(self, state):
        self.calls.append({"kind": "unload"})
        self.alive = False

    async def retire(self, state):
        assert not self.alive
        self.calls.append({"kind": "retire"})

    async def release_ownership(self, **kwargs):
        assert not self.alive

    async def close(self):
        pass

    async def execute_payload(self, dep, payload, attempt, epoch, result_bytes, deadline=None, *, cancelled=None):
        assert self.alive and epoch == self.epoch
        if (deadline is not None and time.time() >= deadline) or (cancelled and cancelled()):
            return {"canceled_before_gpu": True}, "canceled_before_gpu"
        call = {"kind": "execute", "model": dep.model, "attempt": attempt}
        if dep.runtime == "whisper":
            data = base64.b64decode(payload["audio_base64"], validate=True)
            assert data[:4] == b"RIFF" and "audio_ref" not in payload
            call["audio_sha256"] = hashlib.sha256(data).hexdigest()
            result = {"terminal": True, "text": "CPU fixture transcript", "audio_sha256": call["audio_sha256"]}
        else:
            assert not set(payload) & {"urgent", "depends_on", "execution_timeout_seconds"}
            result = {"choices": [{"message": {"role": "assistant", "content": "CPU fixture"}, "finish_reason": "stop"}]}
        self.calls.append(call)
        return result, None

    @contextlib.asynccontextmanager
    async def generate_stream(self, dep, payload, attempt, epoch, *, deadline=None, cancelled=None):
        assert self.alive and epoch == self.epoch
        if (deadline is not None and time.time() >= deadline) or (cancelled and cancelled()):
            raise SchedulerError("canceled_before_gpu", 409)
        gate = self.stream_gates.setdefault(attempt, asyncio.Event())
        self.calls.append({"kind": "stream", "model": dep.model, "attempt": attempt})
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"fixture"}}]}\n\n'
                await gate.wait()
                yield b'data: [DONE]\n\n'
        yield httpx.Response(200, stream=Bytes(), headers={"x-worker-epoch": epoch},
                             request=httpx.Request("POST", "http://cpu-fixture/v1/chat/completions"))


async def executor_main(config, isolation):
    root = Path(config["root"])
    journal = Journal(root, "control-fixture", "executor-fixture")
    asset = root / "weights" / "fixture-model"
    ref = journal.register_asset(asset)
    qwen = Deployment(model="Qwen/Qwen3.5-4B", revision="851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
        runtime="vllm", runtime_version="vllm-0.29.0", image="sha256:" + "a" * 64,
        asset_ref=ref, load=LoadConfig(capacity=2))
    whisper = Deployment(model="openai/whisper-small", revision="973afd24965f72e36ca33b3055d56a652f456b4d",
        runtime="whisper", runtime_version="transformers-5.16.1", image="sha256:" + "b" * 64, asset_ref=ref,
        load=LoadConfig(dtype="float16", attention="sdpa", capacity=1, host_memory_gib=8, shm_gib=1, gpu_memory=.17))
    for dep in (qwen, whisper): journal.register(dep)
    runtime = CPURuntime(root)
    app = Executor(journal, runtime, config["key"])
    async def diagnostic(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/fixture/"):
            assert dict(scope["headers"]).get(b"authorization") == ("Bearer " + config["key"]).encode()
            if scope["path"] == "/fixture/finish" and scope["method"] == "POST":
                for gate in runtime.stream_gates.values(): gate.set()
                value = {"finished": True}
            else:
                with journal.tx() as db:
                    commands = [dict(row) for row in db.execute("SELECT id,state,acknowledged FROM commands")]
                value = {"isolation": isolation, "calls": runtime.calls, "commands": commands}
            await app.reply(send, 200, value)
        else:
            await app(scope, receive, send)
    server = uvicorn.Server(uvicorn.Config(diagnostic, host="127.0.0.1", port=0, loop="asyncio",
        ws="none", lifespan="on", access_log=False, log_level="critical"))
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:
            if serving.done(): await serving
            await asyncio.sleep(.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    print(json.dumps({"ready": True, "url": f"http://127.0.0.1:{port}", "isolation": isolation,
        "manifest": journal.asset(ref)[1], "deployments": [qwen.model_dump(), whisper.model_dump()]}), flush=True)
    await serving


async def control_main(config, isolation):
    import io
    import wave
    root = Path(config["root"])
    key_file = root / "executor-key"
    key_file.write_text(config["key"])
    store = Store(root)
    store.bind_execution("control-fixture", "executor-fixture", config["executor"]["url"], key_file)
    store.assets_import(config["executor"]["manifest"])
    qwen, whisper = [Deployment.model_validate(dep) for dep in config["executor"]["deployments"]]
    for dep in (qwen, whisper): store.register(dep)
    provider = RemoteProvider(store)
    controller = Controller(store, provider)
    await controller.start()
    backend = None
    results = {}
    async def finish_job(dep, body, key):
        job = store.submit(SubmitJob(deployment_id=dep.id, operation="chat" if dep == qwen else "transcribe", input=body), key)[0]
        async with asyncio.timeout(15):
            while store.job(job["id"])["result_state"] != "available":
                await controller.tick()
                await asyncio.sleep(.01)
        await asyncio.gather(*list(controller.tasks.values()))
        return job["id"], json.loads(store.result(job["id"]))
    try:
        payload = {"model": qwen.model, "messages": [{"role": "user", "content": "synthetic"}]}
        jid, value = await finish_job(qwen, payload, "qwen-real-http")
        results["qwen"] = {"job": jid, "result": value}
        backend = RemoteBackend(store, qwen)
        for detach in (False, True):
            attempt = uuid.uuid4().hex
            store.legacy_admit(attempt, qwen.id, store.state()["worker_epoch"], "api-fixture", 30)
            async with backend.generate({**payload, "stream": True}, attempt) as response:
                lines = response.aiter_lines()
                first = await anext(lines)
                assert "fixture" in first and "[DONE]" not in first
                assert store.lease_count() == 1
                if detach:
                    await lines.aclose()
                    store.legacy_detach(attempt, "api-fixture")
                    assert store.health()["detached"] == 1
                    results["detached_lease_held"] = True
                else:
                    await provider.client.post("/fixture/finish")
                    assert [line async for line in lines] == ["data: [DONE]"]
                    results["sse_done"] = True
            if detach:
                # Closing the observer's client cannot terminate the executor producer.
                await backend.close()
                backend = None
                assert store.lease_count() == 1
                await provider.client.post("/fixture/finish")
                async with asyncio.timeout(10):
                    while store.lease_count():
                        await controller.tick()
                        await asyncio.sleep(.01)
            status = await provider.request("GET", "/commands/" + attempt)
            assert status["acknowledged"] and status["state"] == "terminal"
        wav = io.BytesIO()
        with wave.open(wav, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(b"\x00\x00" * 1600)
        data = wav.getvalue()
        ref = store.upload(data)["artifact_ref"]
        jid, value = await finish_job(whisper, {"audio_ref": ref}, "whisper-real-http")
        assert value["audio_sha256"] == hashlib.sha256(data).hexdigest()
        results["whisper"] = {"job": jid, "result": value}
        assert store.lease_count() == 0 and store.execution_pending() == []
        # Recheck both OS boundaries after actual DB/artifacts and engine activity.
        runtime = (await provider.client.get("/fixture/evidence")).json()
        state = store.state()
        await provider.unload(state)
        assert await provider.exited(state)
        store.engine_exited(controller.epoch)
        await controller.cleanup_exited()
        await provider.release_ownership()
        results.update(isolation=isolation, executor=runtime, control_phase=store.state()["phase"],
                       leases=store.lease_count(), pending=store.execution_pending())
        print(json.dumps(results), flush=True)
    finally:
        if backend: await backend.close()
        await controller.close()


async def main():
    config = json.loads(sys.stdin.readline())
    root = Path(config["root"]).resolve()
    try:
        isolation = isolate(root, config["forbidden"])
    except NotImplementedError as exc:
        print(json.dumps({"unsupported": str(exc)}), flush=True)
        return
    if config["role"] == "executor": await executor_main(config, isolation)
    else: await control_main(config, isolation)


if __name__ == "__main__":
    asyncio.run(main())
