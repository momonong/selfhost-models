"""Fixed local execution provider. No client-selected shell, paths or Docker args."""
import asyncio
import copy
from contextlib import asynccontextmanager
from pathlib import Path
import json
import os
import secrets
import sys
import time
import uuid

import httpx

from .backend import VLLMBackend
from .scheduler_schema import SchedulerError, digest
from .scheduler_store import durable_write
from .video import find_video, prepare_video


class DockerRuntime:
    """Executor-local runtime; never opens or depends on the control Store."""

    def __init__(self, root, config):
        self.root, self.config = Path(root).resolve(), config
        self.namespace = digest(str(self.root))[:16]
        self.network = "selfhost-scheduler-" + self.namespace
        self.secret_path = self.root / "worker-key"
        self.backend = None
        self.url = f"http://127.0.0.1:{self.config.worker_port}"
        self.key = None

    async def command(self, *args, timeout=30):
        proc = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise SchedulerError("provider_command_failed", 503)
        if len(out) > 4 * 1024**2:
            raise SchedulerError("provider_response_too_large", 503)
        return out

    async def inventory_identity(self):
        # The runtime currently exposes one physical GPU. Reject an ambiguous
        # multi-GPU host instead of advertising several independent aliases.
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 10)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        devices = out.decode().strip().splitlines()
        if proc.returncode or len(devices) != 1 or not devices[0].startswith("GPU-"):
            raise SchedulerError("single_gpu_identity_required", 503)
        return {"resource_id": "gpu:" + devices[0], "kind": "gpu"}

    async def preflight(self):
        if sys.platform != "linux":
            raise SchedulerError("managed_runtime_requires_linux", 503)
        # SQLite must live on the local Linux filesystem, not drvfs/9p/NAS.
        proc = await asyncio.create_subprocess_exec("stat", "-f", "-c", "%T", str(self.root), stdout=asyncio.subprocess.PIPE)
        fs, _ = await proc.communicate()
        if proc.returncode or fs.strip() not in (b"ext2/ext3", b"xfs", b"btrfs"):
            raise SchedulerError("state_requires_local_linux_filesystem", 503)
        from .gpu_ownership import GPUOwnership
        self.ownership = GPUOwnership(self.root, "managed")
        # Do not strand a new gate while a pre-existing unmanaged owner is live.
        await self.check_other_gpu_owners()
        await asyncio.to_thread(self.ownership.acquire)
        await self.check_other_gpu_owners()
        if not self.secret_path.exists():
            durable_write(self.secret_path, secrets.token_urlsafe(48).encode())
        self.key = self.secret_path.read_text().strip()
        if len(self.key) < 32:
            raise SchedulerError("invalid_worker_secret", 503)
        # Parent directory is private; mounted file readable by fixed worker UID.
        os.chmod(self.root, 0o700)
        os.chmod(self.secret_path, 0o444)

    async def check_other_gpu_owners(self):
        ids = (await self.command("ps", "-q")).decode().split()
        if ids:
            containers = json.loads(await self.command("inspect", *ids))
            for container in containers:
                uses_gpu = container.get("HostConfig", {}).get("DeviceRequests") or container.get("HostConfig", {}).get("Devices")
                if uses_gpu and container.get("Config", {}).get("Labels", {}).get("selfhost.scheduler") != self.namespace:
                    raise SchedulerError("gpu_owner_conflict", 409)

    def handle(self, deployment, epoch):
        return f"selfhost-scheduler-{self.namespace}-{epoch}-{uuid.uuid4().hex[:12]}"

    async def inspect(self, state):
        if not state.get("handle"):
            return None
        raw = json.loads(await self.command("inspect", state["handle"]))
        if len(raw) != 1:
            raise SchedulerError("engine_identity_unconfirmed", 503)
        info = raw[0]
        labels = info.get("Config", {}).get("Labels", {})
        if labels.get("selfhost.scheduler") != self.namespace or labels.get("selfhost.deployment") != state["deployment"]:
            raise SchedulerError("engine_ownership_mismatch", 503)
        if not state['handle'].endswith('-relay') and state.get('container_id') and info['Id'] != state['container_id']:
            raise SchedulerError("engine_identity_unconfirmed", 503)
        return info

    async def exited(self, state):
        # Missing handle, failed inspect, changed frontend epoch are not proof.
        try:
            info = await self.inspect(state)
            return bool(info and info["State"]["Status"] in ("exited", "dead") and not info["State"]["Running"])
        except SchedulerError:
            return False

    async def prepare(self, dep, path):
        """Validate/create CPU-only prerequisites; never issue a worker start."""
        # Recheck other owners immediately before allocating GPU. The deployment
        # migration contract additionally requires the static owner to be stopped.
        await self.preflight()
        if any(c in str(path) + str(self.secret_path) for c in (",", "\n", "\r", "\x00")):
            raise SchedulerError("invalid_mount_path")
        image = json.loads(await self.command("image", "inspect", dep.image))
        if len(image) != 1:
            raise SchedulerError("image_not_available", 503)
        try:
            network = json.loads(await self.command("network", "inspect", self.network))[0]
            if not network.get("Internal") or network.get("Labels", {}).get("selfhost.scheduler") != self.namespace:
                raise SchedulerError("network_ownership_mismatch", 503)
        except SchedulerError as exc:
            if exc.code != "provider_command_failed":
                raise
            await self.command("network", "create", "--internal", "--label", "selfhost.scheduler=" + self.namespace, self.network)

    async def load(self, dep, path, handle):
        # Controller has completed prepare and durably recorded this handle.
        # From this call onward, transport/command failure is conservatively unknown.
        args = ["run", "-d", "--pull=never", "--name", handle, "--label", "selfhost.scheduler=" + self.namespace,
                "--label", "selfhost.deployment=" + dep.id, "--network", self.network,
                "--gpus", "device=0", "--read-only",
                "--cpus", str(dep.load.cpus), "--pids-limit", str(dep.load.pids), "--shm-size", str(dep.load.shm_gib) + "g",
                "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
                "--mount", f"type=bind,source={path},target=/models/current,readonly",
                "--mount", f"type=bind,source={self.secret_path},target=/run/secrets/worker_key,readonly",
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m", "--tmpfs", "/runtime-cache:rw,exec,nosuid,nodev,size=1g,uid=10001,gid=10001",
                "-e", "WORKER_API_KEY_FILE=/run/secrets/worker_key", "-e", "HF_HUB_OFFLINE=1", "-e", "TRANSFORMERS_OFFLINE=1",
                # Official runtime defaults otherwise write under the read-only home.
                # All caches share the existing bounded, worker-writable tmpfs.
                "-e", "XDG_CACHE_HOME=/runtime-cache/cache", "-e", "XDG_CONFIG_HOME=/runtime-cache/config",
                "-e", "HF_HOME=/runtime-cache/cache/huggingface",
                "-e", "VLLM_CACHE_ROOT=/runtime-cache/cache/vllm", "-e", "VLLM_CONFIG_ROOT=/runtime-cache/config/vllm",
                "-e", "TORCHINDUCTOR_CACHE_DIR=/runtime-cache/cache/torchinductor", "-e", "TRITON_CACHE_DIR=/runtime-cache/cache/triton",
                "-e", "FLASHINFER_WORKSPACE_BASE=/runtime-cache",
                "-e", "CUDA_CACHE_PATH=/runtime-cache/cache/cuda", "-e", "TMPDIR=/runtime-cache",
                "-e", "MODEL_ID=" + dep.model, "-e", "MODEL_REVISION=" + dep.revision,
                "-e", "MODEL_PROFILE=qwen3_5", "-e", "VIDEO_ENABLED=" + str(int(dep.load.video)),
                "-e", "VLLM_USE_V2_MODEL_RUNNER=0", "-e", "GPU_MEMORY_UTILIZATION=" + str(dep.load.gpu_memory)]
        if dep.load.host_memory_gib is not None:
            args += ["--memory", str(dep.load.host_memory_gib) + "g"]
        args += [dep.image]
        if dep.runtime == "vllm":
            args += ["/models/current", "--served-model-name", dep.model, "--host", "0.0.0.0", "--port", "8000",
                     "--dtype", dep.load.dtype, "--max-model-len", str(dep.load.context), "--max-num-seqs", str(dep.load.capacity),
                     "--gpu-memory-utilization", str(dep.load.gpu_memory), "--enforce-eager", "--generation-config", "vllm",
                     "--no-enable-log-requests", "--disable-uvicorn-access-log", "--middleware", "identity.WorkerIdentity"]
        await self.command(*args)
        await self.start_relay(dep, handle)
        self.set_backend(dep)
        while True:
            try:
                return await self.identity()
            except (httpx.HTTPError, RuntimeError):
                await asyncio.sleep(0.25)

    async def start_relay(self, dep, handle):
        ingress = self.network + "-ingress"
        try:
            network = json.loads(await self.command("network", "inspect", ingress))[0]
            if network.get("Labels", {}).get("selfhost.scheduler") != self.namespace:
                raise SchedulerError("network_ownership_mismatch", 503)
        except SchedulerError as exc:
            if exc.code != "provider_command_failed":
                raise
            await self.command("network", "create", "--label", "selfhost.scheduler=" + self.namespace, ingress)
        relay = handle + "-relay"
        await self.command("create", "--pull=never", "--name", relay,
            "--label", "selfhost.scheduler=" + self.namespace, "--label", "selfhost.deployment=" + dep.id,
            "--network", ingress, "-p", f"127.0.0.1:{self.config.worker_port}:8000",
            "--read-only", "--memory", "256m", "--cpus", "1", "--pids-limit", "32",
            "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL",
            "--mount", f"type=bind,source={self.secret_path},target=/run/secrets/worker_key,readonly",
            "-e", "WORKER_API_KEY_FILE=/run/secrets/worker_key", "-e", "RELAY_WORKER_URL=http://" + handle + ":8000",
            "--entrypoint", "python3", dep.image, "-m", "worker.relay")
        await self.command("network", "connect", self.network, relay)
        await self.command("start", relay)

    def set_backend(self, dep):
        self.backend = VLLMBackend(self.url, dep.load.capacity + 2, profile="qwen3_5", capacity=dep.load.capacity, video_enabled=dep.load.video)
        self.backend.client.headers["x-selfhost-worker-key"] = self.key

    async def identity(self, dep=None):
        if self.backend is None:
            if dep is None:
                raise SchedulerError("deployment_required", 409)
            self.set_backend(dep)
        return await self.backend.identity()

    async def warmup(self, dep, epoch):
        if dep.runtime == "vllm":
            await self.backend.warmup(dep.model, epoch)
        else:
            response = await self.backend.client.post("/internal/warmup", json={"model": dep.model}, timeout=None)
            response.raise_for_status()
            if response.headers.get("x-worker-epoch") != epoch or response.json().get("terminal") is not True:
                raise SchedulerError("warmup_unconfirmed", 503)

    async def execute_payload(self, dep, payload, request_id, worker_epoch, result_bytes, deadline=None, *, cancelled=None):
        """Execute transferred bytes; deadline is an absolute Unix timestamp.

        Expiration before dispatch is a known non-start. Once dispatched the
        transport must drain to terminal; deadline never proves engine exit.
        """
        payload = copy.deepcopy(payload)
        video = None
        if (deadline is not None and time.time() >= deadline) or (cancelled and cancelled()):
            return {"canceled_before_gpu": True}, "canceled_before_gpu"
        if dep.runtime == "vllm" and find_video(payload):
            video = await prepare_video(payload)
        if (deadline is not None and time.time() >= deadline) or (cancelled and cancelled()):
            return {"canceled_before_gpu": True}, "canceled_before_gpu"
        return await self._execute_prepared(dep, payload, request_id, worker_epoch, result_bytes, video)

    async def _execute_prepared(self, dep, payload, request_id, worker_epoch, result_bytes, video=None):
        if self.backend is None:
            self.set_backend(dep)
        if dep.runtime == "vllm":
            payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
            context = self.backend.generate(payload, request_id)
        else:
            payload["model"] = dep.model
            context = self.backend.client.stream("POST", "/internal/transcribe", json=payload)
        async with context as response:
            if response.headers.get("x-worker-epoch") != worker_epoch:
                raise SchedulerError("worker_changed", 503)
            if response.status_code in (400, 404, 422):
                return {"error": "worker_rejected"}, "worker_rejected"
            response.raise_for_status()
            result = bytearray()
            async for chunk in response.aiter_bytes():
                result.extend(chunk)
                if len(result) > result_bytes:
                    raise SchedulerError("result_too_large", 503)
            data = json.loads(result)
            terminal = (bool(data.get("choices")) and all(c.get("finish_reason") is not None for c in data["choices"])) if dep.runtime == "vllm" else data.get("terminal") is True
            if not terminal:
                raise SchedulerError("terminal_unconfirmed", 503)
            if video:
                data["video"] = video
            return data, None

    @asynccontextmanager
    async def generate_stream(self, dep, payload, request_id, worker_epoch, *, deadline=None, cancelled=None):
        """Yield the verified worker response for the executor's durable spool.

        The caller owns terminal/SSE validation and result limits. It must keep
        consuming independently of its client connection; closing this context
        is not evidence that GPU execution stopped.
        """
        if dep.runtime != "vllm":
            raise SchedulerError("unsupported_operation")
        payload = copy.deepcopy(payload)
        video = await prepare_video(payload) if find_video(payload) else None
        if (deadline is not None and time.time() >= deadline) or (cancelled and cancelled()):
            raise SchedulerError("canceled_before_gpu", 409)
        payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
        if self.backend is None:
            self.set_backend(dep)
        async with self.backend.generate(payload, request_id) as response:
            if response.headers.get("x-worker-epoch") != worker_epoch:
                raise SchedulerError("worker_changed", 503)
            if video:
                response.extensions["selfhost_video"] = video
            yield response

    async def unload(self, state):
        info = await self.inspect(state)
        if info["State"]["Running"]:
            await self.command("stop", "--time", str(self.config.unload_seconds - 1), info["Id"], timeout=self.config.unload_seconds)
        # Relay owns no GPU. Verify labels before stopping its deterministic name.
        relay_state = {**state, "handle": state["handle"] + "-relay"}
        relay = await self.inspect(relay_state)
        if relay["State"]["Running"]:
            await self.command("stop", "--time", "2", relay["Id"])
        # Controller separately verifies entire container exit before release.
        if self.backend:
            await self.backend.close()
            self.backend = None

    async def close(self):
        if self.backend:
            await self.backend.close()

    async def retire(self, state):
        # Invoked only AFTER durable engine-exit accounting. Never force-remove.
        for name in (state["handle"] + "-relay", state["handle"]):
            ids = (await self.command("ps", "-aq", "--filter", "name=^/" + name + "$")).decode().split()
            if not ids:
                continue
            info = await self.inspect({**state, "handle": name})
            if info["State"]["Running"]:
                if not name.endswith("-relay"):
                    raise SchedulerError("cleanup_process_still_running", 409)
                await self.command("stop", "--time", "2", info["Id"])
            await self.command("rm", info["Id"])

    async def release_ownership(self, *, engine_unloaded=False, leases_resolved=False):
        # The executor journal must establish both facts before requesting gate
        # release. GPUOwnership additionally checks actual Docker GPU owners.
        if engine_unloaded is not True or leases_resolved is not True:
            raise SchedulerError("gpu_exit_unconfirmed", 409)
        if not hasattr(self, "ownership"):
            from .gpu_ownership import GPUOwnership
            self.ownership = GPUOwnership(self.root, "managed")
        await asyncio.to_thread(self.ownership.release)


class DockerProvider(DockerRuntime):
    """Compatibility adapter for the original single-host controller/API."""

    def __init__(self, store):
        self.store = store
        super().__init__(store.root, store.config)

    async def identity(self, dep=None):
        if dep is None and self.backend is None:
            dep = self.store.deployment(self.store.state()["deployment"])
        return await super().identity(dep)

    async def execute(self, dep, spec, attempt, store):
        payload = spec.input.model_dump(exclude_none=True)
        video = None
        if dep.runtime == "vllm" and find_video(payload):
            video = await prepare_video(payload)
            # Preserve the local adapter's authoritative cancellation/clock
            # check after decoding and before any GPU dispatch.
            row = store.job(attempt["job"], True)
            if row["cancel_requested"] or store.clock() >= attempt["started"] + row["execution_limit"]:
                return {"canceled_before_gpu": True}, "canceled_before_gpu"
        elif dep.runtime == "whisper":
            import base64
            data, _ = store.artifact(spec.input.audio_ref)
            payload.pop("audio_ref")
            payload["audio_base64"] = base64.b64encode(data).decode()
        return await self._execute_prepared(dep, payload, attempt["id"], attempt["worker_epoch"], store.config.result_bytes, video)

    async def release_ownership(self):
        await super().release_ownership(
            engine_unloaded=self.store.state()["phase"] == "unloaded",
            leases_resolved=self.store.lease_count() == 0,
        )
