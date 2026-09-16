"""CPU feasibility: generate and fully decode bounded synthetic H.264 MP4.

Run via uv run --locked python scripts/video_decode_probe.py.
No GPU calls, downloaded footage, or audio decoding.
"""
import argparse
import io
import json
import math
import time
from pathlib import Path

import av
from PIL import Image, ImageDraw


def make_video(path, seconds, width=640, height=360, fps=30):
    with av.open(str(path), "w", format="mp4") as out:
        stream = out.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        stream.options = {"preset": "ultrafast", "crf": "28", "threads": "1"}
        for index in range(seconds * fps):
            phase = min(2, index * 3 // (seconds * fps))
            image = Image.new("RGB", (width, height), ["red", "green", "blue"][phase])
            ImageDraw.Draw(image).text((20, 20), f"{index / fps:.2f}s", fill="white", font_size=32)
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def inspect_video(path):
    start = time.monotonic()
    raw = path.read_bytes()
    assert len(raw) <= 16 * 1024**2
    with av.open(io.BytesIO(raw), format="mp4", options={"enable_drefs": "0", "use_absolute_path": "0", "protocol_whitelist": ""}) as container:
        assert len(container.streams.video) == 1
        stream = container.streams.video[0]
        stream.thread_type, stream.codec_context.thread_count = "NONE", 1
        fps = float(stream.average_rate)
        duration = float(stream.duration * stream.time_base)
        total = stream.frames
        assert 0 < duration <= 60 and 1 < total <= 3600 and 0 < fps <= 60
        count = min(total, min(120, max(2, math.ceil(duration) * 2)))
        if count % 2:
            count -= 1
        indices = [round(i * (total - 1) / (count - 1)) for i in range(count)]
        samples, pixels, seen, origin = [], 0, 0, None
        for frame in container.decode(stream):
            seen += 1
            assert seen <= 3600 and frame.width <= 1280 and frame.height <= 720
            pixels += frame.width * frame.height
            assert pixels <= 3600 * 1280 * 720
            stamp = float(frame.pts * frame.time_base)
            origin = stamp if origin is None else origin
            stamp -= origin
            assert abs(stamp - (seen - 1) / fps) <= max(float(frame.time_base), 0.0001)
            if seen - 1 in indices:
                samples.append(stamp)
        assert seen == total
    return {"bytes": len(raw), "duration": duration, "source_frames": seen, "fps": fps,
            "sample_timestamps_seconds": samples, "decode_seconds": time.monotonic() - start,
            "decoded_pixels": pixels, "av_version": av.__version__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=Path(".state/video-fixtures"))
    parser.add_argument("--stress", action="store_true", help="also decode the proposed 720p60 upper bound")
    args = parser.parse_args()
    args.fixtures.mkdir(parents=True, exist_ok=True)
    report = {"scope": "CPU MP4 encode/decode only; not GPU or production isolation", "cases": []}
    for duration in [2, 10, 30, 60]:
        path = args.fixtures / f"synthetic-{duration}s.mp4"
        make_video(path, duration)
        result = inspect_video(path)
        report["cases"].append(result)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "sample_timestamps_seconds"}), flush=True)
    if args.stress:
        path = args.fixtures / "synthetic-60s-720p60.mp4"
        make_video(path, 60, 1280, 720, 60)
        result = inspect_video(path)
        result["source_dimensions"] = [1280, 720]
        report["cases"].append(result)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in result.items() if k != "sample_timestamps_seconds"}), flush=True)


if __name__ == "__main__":
    main()
