"""Local registry keyed by HF repo ID; imports never mutate model directories."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from .storage import atomic_json


def repo_key(repo):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo) or ".." in repo:
        raise ValueError("expected HF repo ID: organization/model-name")
    return repo.replace("/", "--")


def default_root():
    return Path(os.getenv("MODEL_ROOT", "D:/hf_models" if os.name == "nt" else "/srv/selfhost-models/models"))


def inventory(path):
    path = Path(path).resolve(strict=True)
    required = {"config.json", "tokenizer_config.json"}
    if (path / "model.safetensors.index.json").is_file():
        idx = json.loads((path / "model.safetensors.index.json").read_text())
        required.add("model.safetensors.index.json")
        required.update(idx["weight_map"].values())
    elif (path / "model.safetensors").is_file():
        required.add("model.safetensors")
    else:
        raise ValueError("missing safetensors weights/index; GGUF is not this backend's input")
    if not (path / "tokenizer.json").is_file() and not (path / "tokenizer.model").is_file():
        raise ValueError("missing tokenizer.json/tokenizer.model")
    required.update(p.name for p in path.iterdir() if p.is_file() and p.suffix in (".json", ".jinja", ".txt", ".model"))
    result = {}
    for name in sorted(required):
        rel = Path(name)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("invalid model shard path")
        f = path / rel
        if not f.is_file() or f.stat().st_size == 0:
            raise ValueError(f"missing/empty model file: {name}")
        stat = f.stat()
        entry = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        if stat.st_size < 50000000:
            entry["sha256"] = hashlib.sha256(f.read_bytes()).hexdigest()
        result[name] = entry
    return result


def infer_revision(path, files):
    revisions = set()
    for name in files:
        metadata = Path(path) / ".cache/huggingface/download" / (name + ".metadata")
        if not metadata.exists():
            raise ValueError("local provenance incomplete; supply --revision with a verified commit SHA")
        revisions.add(metadata.read_text().splitlines()[0])
    if len(revisions) != 1:
        raise ValueError("mixed local revisions; do not infer a fixed revision")
    return revisions.pop()


class Assets:
    def __init__(self, state, root=None):
        self.state = Path(state)
        self.root = Path(root) if root else default_root()
        self.registry = self.state / "models"

    def inspect(self, repo, verify=True):
        f = self.registry / (repo_key(repo) + ".json")
        if not f.exists():
            raise ValueError(f"model not registered: {repo}; use fetch or register first")
        record = json.loads(f.read_text())
        if verify and inventory(record["path"]) != record["files"]:
            raise ValueError("model files changed since registration; inspect provenance before re-registering")
        return record

    def list(self):
        return [json.loads(p.read_text()) for p in sorted(self.registry.glob("*.json"))]

    def register(self, repo, path, revision=None, source="existing"):
        key = repo_key(repo)
        path = Path(path).resolve(strict=True)
        files = inventory(path)
        inferred = revision is None
        revision = revision or infer_revision(path, files)
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("revision must be a full 40-character commit SHA")
        record = {"repo_id": repo, "revision": revision, "path": str(path), "source": source,
                  "provenance": "hf-local-metadata" if inferred else "operator-supplied-commit",
                  "registered_at": datetime.now(timezone.utc).isoformat(), "files": files}
        self.registry.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.registry / (key + ".lock")), timeout=1):
            dest = self.registry / (key + ".json")
            if dest.exists():
                old = self.inspect(repo)
                if any(old[k] != record[k] for k in ("revision", "path", "files")):
                    raise ValueError("repo already registered differently; use a separate --state directory")
                return old
            atomic_json(dest, record)
            if json.loads(dest.read_text()) != record:
                raise OSError("registry readback mismatch")
        return record

    def fetch(self, repo, revision="main"):
        key = repo_key(repo)
        if (self.registry / (key + ".json")).exists():
            return self.inspect(repo)  # No network / latest resolution on repeat.
        from huggingface_hub import HfApi, snapshot_download
        info = HfApi().model_info(repo, revision=revision, token=os.getenv("HF_TOKEN"))
        sha = info.sha
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("HF did not return a fixed commit")
        dest = self.root / "managed" / key / sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(dest) + ".lock", timeout=1):
            if dest.exists():
                raise ValueError(f"existing unregistered destination: {dest}; inspect and register it, no overwrite")
            snapshot_download(repo, revision=sha, local_dir=dest, token=os.getenv("HF_TOKEN"))
            record = self.register(repo, dest, sha, source="hf-fetch")
        return record
