"""Bounded CPU decoder subprocess; never receives client paths or URLs.

The gateway supplies private temporary filenames. Production execution requires
Linux resource limits; decode() is also usable by CPU fixture tests.
"""
import base64
import io
import json
import math
import sys
from pathlib import Path

MAX_BYTES = 16 * 1024**2
MAX_FRAMES = 3600
MAX_PIXELS = 3600 * 1280 * 720
MAX_OUTPUT = 8 * 1024**2


class InvalidVideo(ValueError):
    pass


def require(condition):
    if not condition:
        raise InvalidVideo("invalid_or_unsupported_video")


def decode(path):
    import av
    from PIL import Image, ImageOps

    require(path.stat().st_size <= MAX_BYTES)
    raw = path.read_bytes()
    require(len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12] in
            (b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1"))
    frames, stamps, seen, pixels = [], [], 0, 0
    with av.open(io.BytesIO(raw), format="mp4", options={
        "enable_drefs": "0", "use_absolute_path": "0", "protocol_whitelist": "",
    }) as container:
        require(len(container.streams.video) == 1)
        stream = container.streams.video[0]
        require(stream.codec_context.name == "h264")
        width, height = stream.codec_context.width, stream.codec_context.height
        require(0 < width <= 1280 and 0 < height <= 720)
        require(stream.metadata.get("rotate", "0") in ("0", ""))
        require(stream.average_rate is not None and stream.duration is not None)
        fps = float(stream.average_rate)
        duration = float(stream.duration * stream.time_base)
        total = stream.frames
        require(1 <= duration <= 60 and 0 < fps <= 60 and 2 <= total <= MAX_FRAMES)
        require(abs(duration - total / fps) <= max(float(stream.time_base), 0.0001))
        count = min(total, 120, max(2, math.ceil(duration) * 2))
        count -= count % 2
        indices = [round(i * (total - 1) / (count - 1)) for i in range(count)]
        wanted = set(indices)
        stream.thread_type, stream.codec_context.thread_count = "NONE", 1
        origin = None
        for packet_number, packet in enumerate(container.demux(stream)):
            require(packet_number <= 7200)
            for frame in packet.decode():
                require(seen < MAX_FRAMES and (frame.width, frame.height) == (width, height))
                pixels += width * height
                require(pixels <= MAX_PIXELS and frame.pts is not None)
                require(not getattr(frame, "rotation", 0))
                stamp = float(frame.pts * frame.time_base)
                origin = stamp if origin is None else origin
                stamp -= origin
                require(abs(stamp - seen / fps) <= max(float(frame.time_base), 0.0001))
                if seen in wanted:
                    image = ImageOps.pad(frame.to_image(), (256, 256), method=Image.Resampling.BILINEAR, color="black")
                    buffer = io.BytesIO()
                    image.save(buffer, "JPEG", quality=85)
                    frames.append(base64.b64encode(buffer.getvalue()).decode())
                    stamps.append(stamp)
                seen += 1
        require(seen == total and len(frames) == count)
    model_stamps = [(indices[i] + indices[i + 1]) / (2 * fps) for i in range(0, count, 2)]
    return {"frames": frames, "metadata": {
        "duration_seconds": duration, "source_frames": total, "source_fps": fps,
        "source_width": width, "source_height": height, "sampled_frames": count,
        "frame_indices": indices, "timestamps_seconds": stamps,
        "model_patch_timestamps_seconds": model_stamps,
        "sampling": "uniform_endpoints_2x_ceil_duration_even_v1",
        "frame_width": 256, "frame_height": 256, "resize": "aspect_preserving_black_pad",
        "audio_processed": False, "time_origin": "first_decoded_video_frame",
    }}


def resource_limits():
    import resource  # fail closed on hosts without OS process limits
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 11))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def main():
    resource_limits()
    try:
        result = decode(Path(sys.argv[1]))
    except Exception:
        result = {"error": "invalid_or_unsupported_video"}
    output = json.dumps(result).encode()
    require(len(output) <= MAX_OUTPUT)
    Path(sys.argv[2]).write_bytes(output)


if __name__ == "__main__":
    main()
