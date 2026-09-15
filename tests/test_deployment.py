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
