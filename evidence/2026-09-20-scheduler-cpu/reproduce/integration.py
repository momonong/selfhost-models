"""Disposable CPU evidence: production decoder/execute/ASGI, synthetic HTTP worker."""
import asyncio
import argparse
import base64
import copy
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import time


def worker():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    key = Path(sys.argv[3]).read_text().strip()
    epoch = secrets.token_hex(16)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, status, result):
            raw = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Worker-Epoch", epoch)
            self.end_headers()
            self.wfile.write(raw)
        def allowed(self):
            if self.headers.get("x-selfhost-worker-key") != key:
                self.reply(401, {"error":"unauthorized"})
                return False
            return True
        def do_GET(self):
            if self.allowed():
                self.reply(200, {"epoch":epoch})
        def do_POST(self):
            if not self.allowed():
                return
            size = int(self.headers.get("content-length", "0"))
            if size > 2 * 1024**2:
                self.reply(413, {})
                return
            body = json.loads(self.rfile.read(size) or "{}")
            if self.path == "/internal/warmup":
                self.reply(200, {"terminal":True})
            elif self.path == "/v1/chat/completions":
                parts = body["messages"][0]["content"]
                video = next((x for x in parts if x["type"] == "video_url"), None) if isinstance(parts,list) else None
                self.reply(200, {"choices":[{"message":{"role":"assistant","content":"synthetic CPU fixture"},"finish_reason":"stop"}],
                    "_fixture":{"urgent_absent":"urgent" not in body,"prepared_jpeg": bool(video and video["video_url"]["url"].startswith("data:video/jpeg;base64,")),
                    "frames":body.get("media_io_kwargs",{}).get("video",{}).get("num_frames")}})
            else:
                self.reply(404,{})
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler)
    print("ready", flush=True)
    server.serve_forever()


if len(sys.argv) > 1 and sys.argv[1] == "worker":
    worker()
    sys.exit(0)

parser = argparse.ArgumentParser(description="CPU-only real decoder/TCP fixture; never Docker/GPU")
parser.add_argument("--scratch", type=Path, required=True)
parser.add_argument("--window-seconds", type=int, default=240)
args = parser.parse_args()
RUN_ROOT = args.scratch.resolve(strict=True)
if sys.platform != "linux" or not (RUN_ROOT / ".cpu-validation-snapshot.json").is_file():
    parser.error("--scratch must be a Linux native snapshot created by snapshot.py")
if not 5 <= args.window_seconds <= 600:
    parser.error("--window-seconds must be in [5,600]")
os.chdir(RUN_ROOT)
sys.path[:0] = [str(RUN_ROOT), str(RUN_ROOT / "tests")]
import httpx
import uvicorn
from selfhost_models import video
from selfhost_models.api import Gateway
from selfhost_models.scheduler_controller import Controller
from selfhost_models.scheduler_runtime import DockerProvider
from selfhost_models.scheduler_schema import SubmitJob
from test_scheduler import setup_store
from test_video_decoder import mp4
logging.getLogger("httpx").setLevel(logging.WARNING)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CPUProvider(DockerProvider):
    async def command(self, *args, **kwargs):
        raise AssertionError("Docker is prohibited in the CPU reproduction fixture")
    async def prepare(self, dep, path):
        pass  # CPU-only fixture; never prepare Docker images/networks.
    async def preflight(self):
        # No Docker and no GPU ownership. Only this fixture bypasses lifecycle.
        self.key = self.secret_path.read_text().strip()
    async def load(self, dep, path, handle):
        self.process = await asyncio.create_subprocess_exec(sys.executable, __file__, "worker", str(self.store.config.worker_port), str(self.secret_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        assert (await asyncio.wait_for(self.process.stdout.readline(), 5)).strip() == b"ready"
        self.set_backend(dep)
        return await self.identity()
    async def warmup(self, dep, epoch):
        r = await self.backend.client.post("/internal/warmup", json={})
        assert r.json()["terminal"] and r.headers["x-worker-epoch"] == epoch
    async def exited(self, state):
        return self.process.returncode is not None
    async def stop(self):
        if self.process.returncode is None:
            self.process.terminate()
        await self.process.wait()


async def main():
    root = Path.cwd()
    evidence = {"synthetic_only":True,"gpu":False,"source_manifest":"source-sha256-final.txt","linux_root":str(root)}
    temp = root / "decoder-temp"
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    clip = mp4(root / "synthetic.mp4", seconds=2, fps=10)
    payload = {"model":"Qwen/Qwen3.5-4B","messages":[{"role":"user","content":[{"type":"text","text":"Synthetic sequence"},
        {"type":"video_url","video_url":{"url":video.PREFIX+base64.b64encode(clip.read_bytes()).decode()}}]}]}
    observed = []
    spawned = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec
    async def spy(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        if "selfhost_models.video_decoder" in args:
            observed.append((proc,Path(args[-2]).parent))
            spawned.set()
        return proc
    asyncio.create_subprocess_exec = spy
    def check_child(label):
        proc, path = observed[-1]
        assert proc.returncode is not None and not path.exists()
        try:
            os.waitpid(proc.pid,os.WNOHANG)
        except ChildProcessError:
            reaped = True
        else:
            reaped = False
        assert reaped
        evidence[label] = {"pid":proc.pid,"returncode":proc.returncode,"reaped":True,"temporary_directory_removed":True}

    old_timeout = video.DECODE_SECONDS
    video.DECODE_SECONDS = 0.000001
    try:
        await video.prepare_video(copy.deepcopy(payload),temp_root=temp)
        raise AssertionError("timeout expected")
    except video.VideoError as exc:
        assert exc.code == "video_decode_timeout"
    finally:
        video.DECODE_SECONDS = old_timeout
    check_child("real_decoder_timeout")
    spawned.clear()
    task = asyncio.create_task(video.prepare_video(copy.deepcopy(payload),temp_root=temp))
    await spawned.wait()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check_child("real_decoder_cancel")

    fixture = Path(tempfile.mkdtemp(prefix="integration-state-", dir=root))
    store,q,_,_ = setup_store(fixture,capacity=1,worker_port=free_port())
    q = q.model_copy(update={"load":q.load.model_copy(update={"video":True,"context":8192})})
    store.register(q)
    (store.root/"worker-key").write_text(secrets.token_urlsafe(48))
    (store.root/"worker-key").chmod(0o600)
    key = secrets.token_urlsafe(48)
    key_file = store.root/"api-key"
    key_file.write_text(key)
    key_file.chmod(0o600)
    provider = CPUProvider(store)
    controller = Controller(store,provider)
    await controller.start()
    try:
        spec = SubmitJob.model_validate({"deployment_id":q.id,"operation":"chat","input":payload,"urgent":True})
        job,_ = store.submit(spec,"linux-real-video")
        for _ in range(300):
            await controller.tick()
            await asyncio.sleep(.02)
            if store.job(job["id"])["result_state"] == "available":
                break
        result = json.loads(store.result(job["id"]))
        assert result["_fixture"] == {"urgent_absent":True,"prepared_jpeg":True,"frames":4}
        assert result["video"]["sampled_frames"] == 4
        assert result["video"]["timestamps_seconds"] == [0,0.6,1.3,1.9]
        assert result["video"]["audio_processed"] is False
        assert store.lease_count() == 0
        check_child("production_execute_real_decoder")
        evidence["production_execute"] = {"method_inherited":CPUProvider.execute is DockerProvider.execute,"http_transport":"real TCP subprocess", "video_metadata":result["video"],"worker_received":result["_fixture"],"lease_after_terminal":store.lease_count()}
        port = free_port()
        gateway = Gateway(key=key,scheduler=store)
        server = uvicorn.Server(uvicorn.Config(gateway,host="127.0.0.1",port=port,workers=1,access_log=False,log_level="warning"))
        serving = asyncio.create_task(server.serve())
        while not server.started:
            if serving.done():
                await serving
            await asyncio.sleep(.02)
        stop_file = fixture / "stop-fixture"
        endpoint={"port":port,"api_key_file":str(key_file),"deployment_id":q.id,"model":q.model,"worker_port":store.config.worker_port,"stop_file":str(stop_file)}
        (root/"endpoint.json").write_text(json.dumps(endpoint))
        (root/"integration-evidence.json").write_text(json.dumps(evidence,indent=2))
        print(json.dumps({"server_ready":True,"port":port,"endpoint_file":str(root/"endpoint.json")}),flush=True)
        deadline = time.monotonic()+args.window_seconds
        try:
            while not stop_file.exists() and time.monotonic()<deadline:
                await controller.tick()
                await asyncio.sleep(.025)
        finally:
            server.should_exit = True
            await serving
        evidence["fixture_api_shutdown"]=True
    finally:
        await controller.close()
        await provider.stop()
        evidence["fixture_worker_reaped"] = provider.process.returncode is not None
        asyncio.create_subprocess_exec = real_spawn
        evidence["remaining_decoder_temp_entries"] = len(list(temp.iterdir()))
        (root/"integration-evidence.json").write_text(json.dumps(evidence,indent=2))
        print(json.dumps({"fixture_cleanup":True,"evidence":str(root/"integration-evidence.json")}),flush=True)


asyncio.run(main())
