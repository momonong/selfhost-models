"""Private, versioned single-executor protocol; never a public job schema."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .schema import Chat, StrictModel
from .scheduler_schema import Deployment, Ref, SchedulerError, canonical, digest


class TerminalRejection(SchedulerError):
    """Only constructed after a validated, durably imported terminal receipt."""

Identity = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{8,80}$")]
CommandID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}(?:[0-9a-f]{32})?$")]
Handle = Annotated[str, Field(pattern=r"^selfhost-scheduler-[a-z0-9-]{1,100}$")]


class Grant(StrictModel):
    authority: Identity
    executor: Identity
    fence: Annotated[int, Field(ge=1)]
    handle: Handle
    deployment: Ref
    worker_epoch: Annotated[str, Field(min_length=1, max_length=128)]
    attempt: CommandID
    owner: int | str
    kind: Literal["job", "legacy"]
    started: float
    execution_limit: Annotated[int | float, Field(gt=0, le=1200)]


class AudioExecution(StrictModel):
    model: Literal["openai/whisper-small"]
    audio_base64: Annotated[str, Field(max_length=1398104)]
    audio_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    language: Literal["en", "zh", "ja", "ko", "de", "fr", "es"] | None = None
    max_tokens: Annotated[int, Field(ge=1, le=256)] = 128


class Command(StrictModel):
    version: Literal[1] = 1
    authority: Identity
    executor: Identity
    fence: Annotated[int, Field(ge=1)]
    id: CommandID
    kind: Literal["prepare", "load", "warmup", "execute", "unload", "retire", "release"]
    handle: Handle | None = None
    deployment: Deployment | None = None
    grant: Grant | None = None
    payload: Chat | AudioExecution | None = None
    result_bytes: Annotated[int, Field(ge=1024, le=16777216)] = 2097152

    @model_validator(mode="after")
    def valid_command(self):
        if self.kind != "release" and self.deployment is None:
            raise ValueError("deployment required")
        if self.kind not in ("prepare", "release") and self.handle is None:
            raise ValueError("handle required")
        if self.kind == "execute":
            g, d = self.grant, self.deployment
            if g is None or self.payload is None:
                raise ValueError("execution grant and payload required")
            if (g.authority, g.executor, g.fence, g.handle, g.deployment, g.attempt) != (
                    self.authority, self.executor, self.fence, self.handle, d.id, self.id):
                raise ValueError("grant mismatch")
            if (d.runtime == "vllm") != isinstance(self.payload, Chat) or self.payload.model != d.model:
                raise ValueError("payload deployment mismatch")
            if isinstance(self.payload, Chat) and self.payload.stream and g.kind != "legacy":
                raise ValueError("durable jobs cannot stream")
        elif self.payload is not None or self.grant is not None:
            raise ValueError("unexpected execution payload")
        return self

    @property
    def content_hash(self):
        return digest(self.model_dump())


def receipt_hash(receipt):
    return digest(receipt)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or not manifest or len(manifest) > 10000:
        raise ValueError("invalid manifest")
    for name, entry in manifest.items():
        if (not isinstance(name, str) or name.startswith("/") or "\\" in name or ":" in name
                or any(p in ("", ".", "..") for p in name.split("/"))):
            raise ValueError("invalid asset name")
        if (not isinstance(entry, dict) or set(entry) != {"size", "sha256"}
                or type(entry["size"]) is not int or entry["size"] < 0
                or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in entry["sha256"])):
            raise ValueError("invalid asset entry")
    if len(canonical(manifest)) > 2097152:
        raise ValueError("manifest too large")
    return "asset_" + digest(manifest)
