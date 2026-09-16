"""Capture deployment evidence without secrets, prompts, or host process commands."""
import argparse
import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

if __package__:
    from .deployment_target import validate_target
else:
    from deployment_target import validate_target

ROOT = Path(__file__).resolve().parents[1]


def output(command):
    return subprocess.check_output(command, text=True, encoding="utf-8", timeout=60).strip()


def source_hashes(data):
    return {"sha256": hashlib.sha256(data).hexdigest(),
            "lf_sha256": hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()}


def compare_sources(checkout, deployed, service="API"):
    if checkout.keys() != deployed.keys():
        raise RuntimeError(f"{service} image source file set does not match checkout")
    newline_only = []
    for name, expected in checkout.items():
        actual = deployed[name]
        if expected["lf_sha256"] != actual["lf_sha256"]:
            raise RuntimeError(f"{service} image does not match checkout: {name}")
        if expected["sha256"] != actual["sha256"]:
            newline_only.append(name)
    return sorted(newline_only)


def main(args):
    state = args.state.resolve()
    compose, settings, containers = validate_target(state, args.url)
    snapshot = {"at": datetime.now(timezone.utc).isoformat(), "host": platform.platform(),
                "git_head": output(["git", "rev-parse", "HEAD"]), "url": args.url, "containers": {}}
    snapshot["git_status"] = output(["git", "status", "--short"])
    sources = [ROOT / "compose.yaml", ROOT / "uv.lock", ROOT / "pyproject.toml", ROOT / ".python-version"]
    for directory in ("selfhost_models", "worker", "docker", "scripts", "tests"):
        sources.extend(p for p in (ROOT / directory).rglob("*")
                       if p.is_file() and not {"__pycache__", ".venv"}.intersection(p.parts))
    sources.append(ROOT / "compose.transformers.yaml")
    snapshot["source_sha256"] = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in sorted(sources)}
    for service in ("api", "worker"):
        obj = containers[service]
        snapshot["containers"][service] = {"id": obj["Id"], "image": obj["Config"]["Image"], "image_id": obj["Image"],
            "started_at": obj["State"]["StartedAt"], "running": obj["State"]["Running"],
            "paused": obj["State"]["Paused"], "ports": obj["NetworkSettings"]["Ports"],
            "networks": sorted(obj["NetworkSettings"]["Networks"]),
            "mounts": [{"destination": m["Destination"], "writable": m["RW"]} for m in obj["Mounts"]]}
        image = json.loads(output(["docker", "image", "inspect", obj["Image"]]))[0]
        snapshot["containers"][service]["repo_digests"] = image.get("RepoDigests", [])
    code = "import json,torch,transformers,os,importlib.metadata as m,importlib.util; print(json.dumps({'vllm':m.version('vllm') if importlib.util.find_spec('vllm') else None,'torch':torch.__version__,'cuda':torch.version.cuda,'transformers':transformers.__version__,'runner_v2':os.getenv('VLLM_USE_V2_MODEL_RUNNER')}))"
    snapshot["runtime"] = json.loads(output([*compose, "exec", "-T", "worker", "python3", "-c", code]))
    backend = settings.get("BACKEND", "vllm")
    if backend == "transformers":
        worker_root = "/app"
        worker_files = "[Path('/app/pyproject.toml'), Path('/app/uv.lock'), *Path('/app/worker').rglob('*.py'), *Path('/app/selfhost_models').rglob('*.py')]"
        local_worker = {p.relative_to(ROOT).as_posix(): p for directory in ("worker", "selfhost_models")
                        for p in (ROOT / directory).rglob("*.py") if "__pycache__" not in p.parts}
        local_worker.update({name: ROOT / "worker/transformers" / name for name in ("pyproject.toml", "uv.lock")})
    else:
        worker_root = "/opt/selfhost"
        worker_files = "list(Path('/opt/selfhost').rglob('*.py'))"
        local_worker = {name: ROOT / "worker" / name for name in ("identity.py", "launch.py")}
    worker_code = """
import hashlib, json
from pathlib import Path
files = FILES
def hashes(p):
    data = p.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(),
            'lf_sha256': hashlib.sha256(data.replace(b'\\r\\n', b'\\n')).hexdigest()}
print(json.dumps({p.relative_to(BASE).as_posix(): hashes(p) for p in files}))
""".replace("FILES", worker_files).replace("BASE", repr(worker_root))
    hashes = json.loads(output([*compose, "exec", "-T", "worker", "python3", "-c", worker_code]))
    expected_worker = {name: source_hashes(p.read_bytes()) for name, p in local_worker.items()}
    newline_only = compare_sources(expected_worker, hashes, "worker")
    snapshot["checkout_worker_source_hashes"] = expected_worker
    snapshot["worker_source_hashes"] = hashes
    snapshot["worker_source_sha256"] = {name: item["sha256"] for name, item in hashes.items()}
    snapshot["worker_source_matches_checkout"] = not newline_only
    snapshot["worker_source_text_matches_checkout"] = True
    snapshot["worker_source_newline_only_differences"] = newline_only
    api_code = """
import hashlib, importlib.metadata as m, importlib.util, json, sys
from pathlib import Path
files = [Path('/app/pyproject.toml'), Path('/app/uv.lock'), *Path('/app/selfhost_models').rglob('*.py')]
def hashes(p):
    data = p.read_bytes()
    return {'sha256': hashlib.sha256(data).hexdigest(),
            'lf_sha256': hashlib.sha256(data.replace(b'\\r\\n', b'\\n')).hexdigest()}
print(json.dumps({'python': sys.version.split()[0], 'prefix': sys.prefix,
    'pytest_present': importlib.util.find_spec('pytest') is not None,
    'packages': {p: m.version(p) for p in ('selfhost-models', 'httpx', 'pydantic', 'uvicorn', 'huggingface-hub', 'pillow', 'av')},
    'source_sha256': {p.relative_to('/app').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    'source_hashes': {p.relative_to('/app').as_posix(): hashes(p) for p in files}}))
"""
    snapshot["api_runtime"] = json.loads(output([*compose, "exec", "-T", "api", "python", "-c", api_code]))
    api_sources = [ROOT / "pyproject.toml", ROOT / "uv.lock", *(ROOT / "selfhost_models").rglob("*.py")]
    snapshot["checkout_api_source_hashes"] = {
        p.relative_to(ROOT).as_posix(): source_hashes(p.read_bytes()) for p in api_sources}
    newline_only = compare_sources(snapshot["checkout_api_source_hashes"], snapshot["api_runtime"]["source_hashes"])
    snapshot["api_source_matches_checkout"] = not newline_only
    snapshot["api_source_text_matches_checkout"] = True
    snapshot["api_source_newline_only_differences"] = newline_only
    snapshot["source_comparison"] = "raw SHA256 retained; text comparison normalizes CRLF to LF only"
    snapshot["gpu"] = output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,memory.free", "--format=csv"])
    with httpx.Client(base_url=args.url, trust_env=False, timeout=5) as client:
        response = client.get("/health/ready")
        response.raise_for_status()
        snapshot["health"] = response.json()
        client.headers["Authorization"] = "Bearer " + (state / "api-key").read_text().strip()
        response = client.get("/v1/models")
        response.raise_for_status()
        snapshot["models"] = response.json()
        assert snapshot["models"]["data"][0]["backend"] == backend
    snapshot["settings"] = {k: v.strip().strip("'") for k, v in
        (line.split("=", 1) for line in (state / "compose.env").read_text().splitlines())
        if k in ("BACKEND", "MAX_INFLIGHT", "MAX_MODEL_LEN", "GPU_MEMORY_UTILIZATION", "DEADLINE_SECONDS", "DRAIN_SECONDS",
                 "API_PORT", "GPU_DEVICE", "API_UID", "API_GID", "VLLM_USE_V2_MODEL_RUNNER")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps(snapshot, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--output", type=Path, default=ROOT / "evidence/runtime.json")
    main(parser.parse_args())
