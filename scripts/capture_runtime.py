"""Capture deployment evidence without secrets, prompts, or host process commands."""
import argparse
import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--env-file", str(ROOT / ".state/compose.env"), "-f", str(ROOT / "compose.yaml")]


def output(command):
    return subprocess.check_output(command, text=True, timeout=60).strip()


def main(path):
    snapshot = {"at": datetime.now(timezone.utc).isoformat(), "host": platform.platform(),
                "git_head": output(["git", "rev-parse", "HEAD"]), "containers": {}}
    snapshot["git_status"] = output(["git", "status", "--short"])
    sources = [ROOT / "compose.yaml", ROOT / "uv.lock", ROOT / "pyproject.toml", ROOT / ".python-version"]
    for directory in ("selfhost_models", "worker", "docker", "scripts", "tests"):
        sources.extend(p for p in (ROOT / directory).rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts)
    snapshot["source_sha256"] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(sources)}
    for service in ("api", "worker"):
        identifier = output([*COMPOSE, "ps", "-q", service])
        obj = json.loads(output(["docker", "inspect", identifier]))[0]
        snapshot["containers"][service] = {"id": obj["Id"], "image": obj["Config"]["Image"], "image_id": obj["Image"],
            "started_at": obj["State"]["StartedAt"], "running": obj["State"]["Running"],
            "paused": obj["State"]["Paused"], "ports": obj["NetworkSettings"]["Ports"],
            "networks": sorted(obj["NetworkSettings"]["Networks"]),
            "mounts": [{"destination": m["Destination"], "writable": m["RW"]} for m in obj["Mounts"]]}
    code = "import json,torch,vllm,transformers,os; print(json.dumps({'vllm':vllm.__version__,'torch':torch.__version__,'cuda':torch.version.cuda,'transformers':transformers.__version__,'runner_v2':os.environ['VLLM_USE_V2_MODEL_RUNNER']}))"
    snapshot["runtime"] = json.loads(output([*COMPOSE, "exec", "-T", "worker", "python3", "-c", code]))
    api_code = """
import hashlib, importlib.metadata as m, importlib.util, json, sys
from pathlib import Path
files = [Path('/app/pyproject.toml'), Path('/app/uv.lock'), *Path('/app/selfhost_models').glob('*.py')]
print(json.dumps({'python': sys.version.split()[0], 'prefix': sys.prefix,
    'pytest_present': importlib.util.find_spec('pytest') is not None,
    'packages': {p: m.version(p) for p in ('selfhost-models', 'httpx', 'pydantic', 'uvicorn', 'huggingface-hub', 'pillow')},
    'source_sha256': {p.relative_to('/app').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}))
"""
    snapshot["api_runtime"] = json.loads(output([*COMPOSE, "exec", "-T", "api", "python", "-c", api_code]))
    for name, digest in snapshot["api_runtime"]["source_sha256"].items():
        if snapshot["source_sha256"].get(name) != digest:
            raise RuntimeError(f"API image does not match checkout: {name}")
    snapshot["api_source_matches_checkout"] = True
    snapshot["gpu"] = output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,memory.free", "--format=csv"])
    with httpx.Client(base_url="http://127.0.0.1:18080", trust_env=False, timeout=5) as client:
        snapshot["health"] = client.get("/health/ready").json()
        client.headers["Authorization"] = "Bearer " + (ROOT / ".state/api-key").read_text().strip()
        snapshot["models"] = client.get("/v1/models").json()
    snapshot["settings"] = {k: v.strip().strip("'") for k, v in
        (line.split("=", 1) for line in (ROOT / ".state/compose.env").read_text().splitlines())
        if k in ("MAX_INFLIGHT", "MAX_MODEL_LEN", "GPU_MEMORY_UTILIZATION", "DEADLINE_SECONDS", "DRAIN_SECONDS")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/runtime.json")
    main(parser.parse_args().output)
