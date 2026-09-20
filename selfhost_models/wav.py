"""A deliberately small PCM WAV surface: no native decoder or external sources."""
import struct

from .scheduler_schema import SchedulerError


def validate_wav(data):
    # Canonical 44-byte PCM header; ancillary chunks and trailing data are rejected.
    if not 44 < len(data) <= 1048576 or data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt ":
        raise SchedulerError("invalid_wav")
    size, fmt_size = struct.unpack_from("<I", data, 4)[0], struct.unpack_from("<I", data, 16)[0]
    fmt, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", data, 20)
    frames = struct.unpack_from("<I", data, 40)[0]
    if (size != len(data)-8 or fmt_size != 16 or (fmt,channels,rate,byte_rate,align,bits) != (1,1,16000,32000,2,16)
            or data[36:40] != b"data" or frames != len(data)-44 or frames % 2 or not 0 < frames <= 960000):
        raise SchedulerError("invalid_wav")
    return {"duration_seconds": frames / 32000, "samples": frames // 2}
