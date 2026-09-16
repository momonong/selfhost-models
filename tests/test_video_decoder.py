import io
from pathlib import Path

import av
import pytest
from PIL import Image

from selfhost_models.video_decoder import InvalidVideo, decode


def mp4(path, *, seconds=2, fps=10, size=(320, 180), codec="libx264", vfr=False, audio=False):
    with av.open(str(path), "w", format="mp4") as out:
        video = out.add_stream(codec, rate=fps)
        video.width, video.height, video.pix_fmt = *size, "yuv420p"
        video.options = {"threads": "1", "preset": "ultrafast"}
        sound = out.add_stream("aac", rate=48000) if audio else None
        for index in range(seconds * fps):
            frame = av.VideoFrame.from_image(Image.new("RGB", size, "red" if index < seconds * fps // 2 else "blue"))
            frame.pts = index + (1 if vfr and index >= fps else 0)
            for packet in video.encode(frame):
                out.mux(packet)
        for packet in video.encode():
            out.mux(packet)
        if sound:
            for index in range((seconds * 48000 + 1023) // 1024):
                frame = av.AudioFrame(format="fltp", layout="mono", samples=1024)
                frame.sample_rate, frame.pts = 48000, index * 1024
                for plane in frame.planes:
                    plane.update(bytes(plane.buffer_size))
                for packet in sound.encode(frame):
                    out.mux(packet)
            for packet in sound.encode():
                out.mux(packet)
    return path


def test_real_mp4_sampling_and_audio_ignored(tmp_path):
    import base64
    clean = decode(mp4(tmp_path / "video.mp4"))
    with_audio = decode(mp4(tmp_path / "audio.mp4", audio=True))
    assert clean == with_audio
    meta = clean["metadata"]
    assert meta["timestamps_seconds"] == [0, 0.6, 1.3, 1.9]
    assert meta["model_patch_timestamps_seconds"] == [0.3, 1.6]
    assert meta["sampled_frames"] == 4 and meta["audio_processed"] is False
    first = Image.open(io.BytesIO(base64.b64decode(clean["frames"][0])))
    last = Image.open(io.BytesIO(base64.b64decode(clean["frames"][-1])))
    assert first.size == last.size == (256, 256)
    assert first.getpixel((128, 128))[0] > 200
    assert last.getpixel((128, 128))[2] > 200


@pytest.mark.parametrize("options", [
    {"seconds": 61, "fps": 2}, {"size": (1296, 32)}, {"fps": 61},
    {"vfr": True}, {"codec": "mpeg4"},
])
def test_declared_limits_and_vfr_are_rejected(tmp_path, options):
    path = mp4(tmp_path / "bad.mp4", **options)
    with pytest.raises(InvalidVideo):
        decode(path)


def test_corrupt_and_oversized_files(tmp_path):
    path = tmp_path / "bad.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises((InvalidVideo, av.error.FFmpegError)):
        decode(path)
    with path.open("wb") as file:
        file.truncate(16 * 1024**2 + 1)
    with pytest.raises(InvalidVideo):
        decode(path)


def test_sixty_seconds_selects_both_endpoints(tmp_path):
    result = decode(mp4(tmp_path / "minute.mp4", seconds=60, fps=2, size=(64, 64)))
    assert result["metadata"]["sampled_frames"] == 120
    assert result["metadata"]["timestamps_seconds"] == [i / 2 for i in range(120)]


def test_quicktime_brand_is_not_accepted_as_mp4(tmp_path):
    path = mp4(tmp_path / "quicktime.mp4")
    raw = bytearray(path.read_bytes())
    raw[8:12] = b"qt  "
    path.write_bytes(raw)
    with pytest.raises(InvalidVideo):
        decode(path)
