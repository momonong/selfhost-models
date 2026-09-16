"""Synthetic, sequential fixed-runtime probe. No business footage or audio.

CPU mode only instantiates the processor. GPU mode bypasses the public gateway
for a worker capability experiment and REQUIRES an exclusive maintenance window.
Never use this script while product clients are using the service.
"""
import argparse
import base64
import io
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw
import httpx


def fixture(duration, count, edge):
    total = duration * 30
    indices = [round(i * (total - 1) / (count - 1)) for i in range(count)]
    frames = []
    for index in indices:
        phase = min(2, index * 3 // total)
        image = Image.new("RGB", (edge, edge), ["red", "green", "blue"][phase])
        ImageDraw.Draw(image).text((15, 15), f"{index / 30:.2f}s", fill="white", font_size=24)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=85)
        frames.append(base64.b64encode(buffer.getvalue()).decode())
    metadata = {"fps": 30.0, "total_num_frames": total, "duration": duration,
                "frames_indices": indices, "do_sample_frames": False}
    return frames, metadata


CPU = '''
import base64, io, json, sys, time
import numpy as np
from PIL import Image
from transformers import AutoProcessor
item = json.load(sys.stdin)
frames = np.stack([np.asarray(Image.open(io.BytesIO(base64.b64decode(x)))) for x in item['frames']])
p = AutoProcessor.from_pretrained('/models/current', local_files_only=True, trust_remote_code=False)
text = p.apply_chat_template([{'role':'user','content':[{'type':'video'}, {'type':'text','text':'List the colors in time order.'}]}], tokenize=False, add_generation_prompt=True, enable_thinking=False)
meta = {k:v for k,v in item['metadata'].items() if k != 'do_sample_frames'}
t = time.monotonic()
out = p(text=[text], videos=[frames], video_metadata=[meta], do_sample_frames=False, return_tensors='pt')
print(json.dumps({'input_tokens':out['input_ids'].shape[-1], 'grid':out['video_grid_thw'].tolist(), 'seconds':time.monotonic()-t, 'template':text}))
'''

GPU = '''
import json, sys, time, httpx
p = json.load(sys.stdin)
t = time.monotonic()
with httpx.Client(timeout=120, trust_env=False) as c:
    r = c.post('http://worker:8000/v1/chat/completions', json=p)
    print(json.dumps({'status':r.status_code,'seconds':time.monotonic()-t,'body':r.json()}))
'''


def execute(container, script, data):
    result = subprocess.run(["docker", "exec", "-i", container, "python3", "-c", script],
                            input=json.dumps(data), capture_output=True, text=True, encoding="utf-8", timeout=150)
    if result.returncode:
        return {"error": result.stderr[-4000:], "exit_code": result.returncode}
    return json.loads(result.stdout)


def gpu_sample(stop, samples):
    while not stop.is_set():
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            samples.append({"monotonic": time.monotonic(), "values": r.stdout.strip()})
        stop.wait(0.25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu-exclusive-maintenance", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("use a new evidence filename; preserve previous attempts")
    if args.gpu_exclusive_maintenance:
        with httpx.Client(trust_env=False, timeout=5) as client:
            health = client.get("http://127.0.0.1:18080/health/ready").json()
        if not health["ready"] or any(health[k] for k in ("inflight", "detached", "uncertain")):
            raise SystemExit("exclusive probe requires a ready, empty gateway")
    report = {"at": datetime.now(timezone.utc).isoformat(), "mode": "gpu" if args.gpu_exclusive_maintenance else "processor-only",
              "source": "synthetic presampled frames; MP4 decode is a separate acceptance requirement", "cases": []}
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as file:
            json.dump(report, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
    cases = [(2, 8, 256), (10, 16, 256), (30, 16, 256), (60, 16, 256),
             (60, 32, 256), (60, 16, 384), (60, 120, 256)]
    if args.gpu_exclusive_maintenance:
        cases = [(2, 4, 256), (10, 20, 256), (30, 60, 256), (60, 120, 256),
                 (60, 32, 256), (60, 16, 384)]
    for duration, count, edge in cases:
        frames, metadata = fixture(duration, count, edge)
        case = {"duration": duration, "frames": count, "edge": edge, "metadata": metadata,
                "sample_timestamps_seconds": [i / 30 for i in metadata["frames_indices"]]}
        report["cases"].append(case)
        if args.gpu_exclusive_maintenance:
            payload = {"model": "Qwen/Qwen3.5-4B", "messages": [{"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": "data:video/jpeg;base64," + ",".join(frames)}},
                {"type": "text", "text": "List the background colors in chronological order and approximate time ranges."}]}],
                "max_tokens": 96, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False},
                "media_io_kwargs": {"video": {**metadata, "num_frames": count}},
                "mm_processor_kwargs": {"do_sample_frames": False, "max_pixels": 120 * 256 * 256}}
            samples, stop = [], threading.Event()
            sampler = threading.Thread(target=gpu_sample, args=(stop, samples))
            sampler.start()
            case["terminal_confirmed"] = False
            save()
            try:
                case["result"] = execute("selfhost-models-api-1", GPU, payload)
            finally:
                stop.set()
                sampler.join(6)
            case["gpu_samples"] = samples
            case["gpu_sample_scope"] = "host-wide nvidia-smi memory.used MiB, memory.free MiB, utilization percent; not allocator attribution"
            case["gpu_after"] = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used,memory.free,utilization.gpu", "--format=csv,noheader"], text=True).strip()
            result = case["result"]
            choices = result.get("body", {}).get("choices", [])
            case["terminal_confirmed"] = result.get("status") in (400, 404, 422) or (
                result.get("status") == 200 and bool(choices) and all(c.get("finish_reason") for c in choices))
            save()
            if not case["terminal_confirmed"]:
                raise SystemExit("unconfirmed worker request; stop probing and recover worker before reuse")
        else:
            case["result"] = execute("selfhost-models-worker-1", CPU, {"frames": frames, "metadata": metadata})
        save()
        print(json.dumps({k: v for k, v in case.items() if k not in ("metadata", "sample_timestamps_seconds", "gpu_samples")}), flush=True)


if __name__ == "__main__":
    main()
