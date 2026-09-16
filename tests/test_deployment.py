from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_deployment_isolation_contract():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    api, worker = config["services"]["api"], config["services"]["worker"]
    assert all(p.startswith("127.0.0.1:") for p in api["ports"])
    assert "ports" not in worker
    assert config["networks"]["inference"]["internal"] is True
    assert worker["volumes"][0]["read_only"] is True
    assert worker["volumes"][0]["bind"]["create_host_path"] is False
    assert "docker.sock" not in str(api)
    assert "HF_TOKEN" not in str(config)
    assert "--no-enable-log-requests" in worker["command"]
    assert worker["environment"]["VLLM_USE_V2_MODEL_RUNNER"] == "${VLLM_USE_V2_MODEL_RUNNER:-0}"
    assert set(api["networks"]) == {"ingress", "inference"}
    assert worker["networks"] == ["inference"]
    assert api["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=128m"]
    assert api["pids_limit"] == 64 and api["cpus"] == 2


def test_transformers_override_keeps_runtime_isolated_and_bounds_kernel_cache():
    worker = yaml.safe_load((ROOT / "compose.transformers.yaml").read_text())["services"]["worker"]
    assert worker["build"]["dockerfile"] == "docker/transformers.Dockerfile"
    assert worker["environment"]["MAX_INFLIGHT"] == "1"
    assert worker["stop_grace_period"] == "30s"
    assert worker["read_only"] and worker["cap_drop"] == ["ALL"]
    assert "ports" not in worker and "volumes" not in worker  # Inherits read-only model/internal network.
    cache = next(m for m in worker["tmpfs"] if m.startswith("/runtime-cache:"))
    assert all(option in cache for option in ("exec", "nosuid", "nodev", "size=1g"))
