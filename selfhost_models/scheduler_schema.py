"""Strict public jobs and operator-only deployment configuration.

No public schema contains a path, URL, command, image, or runtime override.
"""
import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .schema import Chat, StrictModel

Ref = Annotated[str, Field(pattern=r"^[a-z]+_[0-9a-f]{64}$")]
JobID = Annotated[str, Field(pattern=r"^job_[0-9a-f]{32}$")]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class LoadConfig(StrictModel):
    dtype: Literal["bfloat16", "float16"] = "bfloat16"
    attention: Literal["runtime", "sdpa"] = "runtime"
    context: Annotated[int, Field(ge=128, le=8192)] = 2048
    capacity: Annotated[int, Field(ge=1, le=16)] = 2
    gpu_memory: Annotated[float, Field(gt=0, le=0.9)] = 0.6
    video: bool = False
    eager: Literal[True] = True
    device: Literal["0"] = "0"
    host_memory_gib: Annotated[int, Field(ge=1, le=64)] | None = None
    shm_gib: Annotated[int, Field(ge=1, le=8)] = 2
    cpus: Annotated[int, Field(ge=1, le=6)] = 4
    pids: Annotated[int, Field(ge=64, le=512)] = 128


class Deployment(StrictModel):
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    asset_ref: Ref
    runtime: Literal["vllm", "whisper"]
    image: Annotated[str, Field(pattern=r"^(?:[A-Za-z0-9./:_-]+@)?sha256:[0-9a-f]{64}$")]
    runtime_version: Literal["vllm-0.29.0", "transformers-5.16.1"]
    adapter_version: Literal["scheduler-v1"] = "scheduler-v1"
    quantization: Literal["none"] = "none"
    load: LoadConfig = Field(default_factory=LoadConfig)

    @model_validator(mode="after")
    def supported(self):
        if self.runtime == "vllm":
            if (self.model != "Qwen/Qwen3.5-4B" or
                    self.revision != "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a" or
                    self.runtime_version != "vllm-0.29.0" or self.load.dtype != "bfloat16" or
                    self.load.attention != "runtime"):
                raise ValueError("unsupported Qwen deployment")
            if self.load.video and (self.load.context != 8192 or self.load.capacity > 2):
                raise ValueError("video requires context 8192 and capacity <=2")
        elif (self.model != "openai/whisper-small" or
                self.revision != "973afd24965f72e36ca33b3055d56a652f456b4d" or
                self.runtime_version != "transformers-5.16.1" or self.load.dtype != "float16" or
                self.load.attention != "sdpa" or self.load.capacity != 1 or self.load.video or
                self.load.host_memory_gib is None or self.load.host_memory_gib > 8):
            raise ValueError("unsupported Whisper deployment")
        return self

    @property
    def id(self):
        return "dep_" + digest(self.model_dump())


class AudioInput(StrictModel):
    audio_ref: Ref
    language: Literal["en", "zh", "ja", "ko", "de", "fr", "es"] | None = None
    max_tokens: Annotated[int, Field(ge=1, le=256)] = 128


class SubmitJob(StrictModel):
    deployment_id: Ref
    operation: Literal["chat", "transcribe"]
    input: Chat | AudioInput
    urgent: bool = False
    depends_on: Annotated[list[JobID], Field(max_length=32)] = Field(default_factory=list)
    queue_timeout_seconds: Annotated[int, Field(ge=1, le=86400)] = 3600
    execution_timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 60
    result_description: Annotated[str, Field(max_length=512)] | None = None

    @model_validator(mode="after")
    def operation_input(self):
        if (self.operation == "chat") != isinstance(self.input, Chat):
            raise ValueError("operation and input mismatch")
        if isinstance(self.input, Chat) and self.input.stream:
            raise ValueError("async chat does not stream")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate dependency")
        return self


class Upload(StrictModel):
    media_type: Literal["audio/wav"]
    data_base64: Annotated[str, Field(max_length=1398104)]
    description: Annotated[str, Field(max_length=512)] | None = None


class SchedulerConfig(StrictModel):
    queue_capacity: Annotated[int, Field(ge=1, le=10000)] = 256
    retained_jobs: Annotated[int, Field(ge=1, le=100000)] = 4096
    storage_bytes: Annotated[int, Field(ge=4194304, le=1099511627776)] = 1024**3
    input_bytes: Annotated[int, Field(ge=1024, le=25165824)] = 25165824
    result_bytes: Annotated[int, Field(ge=1024, le=16777216)] = 2097152
    retention_seconds: Annotated[int, Field(ge=60, le=31536000)] = 604800
    reuse_dispatches: Annotated[int, Field(ge=1, le=100)] = 3
    aging_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    load_seconds: Annotated[int, Field(ge=1, le=600)] = 180
    warmup_seconds: Annotated[int, Field(ge=1, le=600)] = 180
    drain_seconds: Annotated[int, Field(ge=1, le=1200)] = 600
    unload_seconds: Annotated[int, Field(ge=1, le=600)] = 60
    worker_port: Annotated[int, Field(ge=1024, le=65535)] = 18089
    max_load_attempts: Annotated[int, Field(ge=1, le=100000)] = 10000
    max_generation_attempts: Annotated[int, Field(ge=1, le=10000000)] = 100000


class SchedulerError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)
