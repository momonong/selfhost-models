"""Video preparation runs only under a durable gateway lease."""
import asyncio
import base64
import binascii
import json
import os
import sys
import tempfile
from pathlib import Path

from .video_decoder import MAX_BYTES, MAX_OUTPUT

PREFIX = "data:video/mp4;base64,"
MAX_URI = len(PREFIX) + 4 * ((MAX_BYTES + 2) // 3)
DECODE_SECONDS = 15
VIDEO_PIXELS = 120 * 256 * 256
LINUX_DECODER = sys.platform == "linux"
LIMITS = {"container": "mp4", "codec": "h264", "max_bytes": MAX_BYTES,
          "min_duration_seconds": 1, "max_duration_seconds": 60,
          "max_width": 1280, "max_height": 720, "max_fps": 60,
          "constant_frame_rate_required": True, "max_source_frames": 3600,
          "max_sampled_frames": 120, "frame_width": 256, "frame_height": 256,
          "sampling": "uniform_endpoints_2x_ceil_duration_even_v1", "audio": False}


class VideoError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def find_video(payload):
    for message in payload.get("messages", []):
        if isinstance(message.get("content"), list):
            for part in message["content"]:
                if part["type"] == "video_url":
                    return part
    return None


async def prepare_video(payload, *, temp_root=None):
    """Await child termination AND temporary cleanup before returning/raising.

    Caller owns admission and must not cancel this task on client disconnect.
    On gateway shutdown we kill/wait the CPU child; unknown GPU work continues
    to follow the separate durable worker lease contract.
    """
    part = find_video(payload)
    if part is None:
        return None
    if not LINUX_DECODER:
        raise VideoError(400, "video_requires_linux_decoder")
    try:
        raw = base64.b64decode(part["video_url"]["url"][len(PREFIX):], validate=True)
    except (ValueError, binascii.Error):
        raise VideoError(400, "invalid_video_base64") from None
    if len(raw) > MAX_BYTES:
        raise VideoError(400, "video_too_large")
    with tempfile.TemporaryDirectory(prefix="selfhost-video-", dir=temp_root) as directory:
        source, target = Path(directory) / "input.mp4", Path(directory) / "output.json"
        source.write_bytes(raw)
        del raw
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "selfhost_models.video_decoder", str(source), str(target),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k in ("PATH", "PYTHONPATH", "LANG", "SYSTEMROOT")},
        )
        try:
            try:
                await asyncio.wait_for(process.wait(), DECODE_SECONDS)
            except TimeoutError:
                raise VideoError(504, "video_decode_timeout") from None
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.shield(process.wait())
        if process.returncode != 0 or not target.exists() or target.stat().st_size > MAX_OUTPUT:
            raise VideoError(400, "video_decode_limit")
        try:
            result = json.loads(target.read_bytes())
            if "error" in result:
                raise VideoError(400, result["error"])
            frames, meta = result["frames"], result["metadata"]
        except (ValueError, KeyError):
            raise VideoError(400, "invalid_video_output") from None
    # The MP4 and decoder output are gone before GPU dispatch.
    part["video_url"]["url"] = "data:video/jpeg;base64," + ",".join(frames)
    payload["media_io_kwargs"] = {"video": {
        "num_frames": meta["sampled_frames"], "fps": meta["source_fps"],
        "frames_indices": meta["frame_indices"], "total_num_frames": meta["source_frames"],
        "duration": meta["duration_seconds"], "do_sample_frames": False,
    }}
    payload["mm_processor_kwargs"] = {"do_sample_frames": False, "max_pixels": VIDEO_PIXELS}
    return meta
