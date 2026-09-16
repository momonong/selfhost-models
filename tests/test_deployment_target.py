"""A custom URL, key and fault target must describe one deployment."""
import copy
import hashlib
import json

import pytest

from scripts import deployment_target as target


@pytest.mark.parametrize("backend", ["vllm", "transformers"])
@pytest.mark.parametrize("drift", [None, "url", "key", "state", "backend", "port", "project", "paused", "pinned-image", "wrong-image"])
def test_target_matches_or_rejects_before_faults(tmp_path, monkeypatch, backend, drift):
    state = tmp_path / "custom-state"
    state.mkdir()
    (state / "compose.env").write_text(f"BACKEND='{backend}'\nAPI_PORT='18081'\n")
    (state / "api-key").write_text("synthetic-test-key")
    config = {"services": {service: {"image": f"image-{service}", "environment": {"BACKEND": backend}}
                           for service in ("api", "worker")}}
    containers = {service: {"Id": service, "Image": f"sha256:{service}", "Config": {
        "Image": f"image-{service}", "Env": [f"BACKEND={backend}"],
        "Labels": {"com.docker.compose.project": "selfhost-models", "com.docker.compose.service": service,
                   "com.docker.compose.config-hash": f"hash-{service}"}},
        "State": {"Running": True, "Paused": False},
        "NetworkSettings": {"Ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18081"}]}}}
        for service in ("api", "worker")}
    if drift == "state":
        containers["api"]["Config"]["Labels"]["com.docker.compose.config-hash"] = "another-state"
    elif drift == "backend":
        containers["worker"]["Config"]["Env"] = ["BACKEND=another-backend"]
    elif drift == "port":
        containers["api"]["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"] = "18080"
    elif drift == "project":
        containers["worker"]["Config"]["Labels"]["com.docker.compose.project"] = "unrelated"
    elif drift == "paused":
        containers["worker"]["State"]["Paused"] = True
    elif drift in ("pinned-image", "wrong-image"):
        containers["worker"]["Config"]["Image"] = "image-worker@sha256:worker"
        if drift == "wrong-image":
            containers["worker"]["Image"] = "sha256:unrelated"
    calls = []

    def output(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "inspect"]:
            return json.dumps([copy.deepcopy(containers[command[-1]])])
        if command[:3] == ["docker", "image", "inspect"]:
            return "sha256:worker"
        assert str(state / "compose.env") in command
        assert ("compose.transformers.yaml" in " ".join(command)) == (backend == "transformers")
        if "--format" in command:
            return json.dumps(config)
        if "--hash" in command:
            if drift == "pinned-image" and command[-1] == "worker":
                assert json.loads(kwargs["input"])["services"]["worker"]["image"] == "image-worker@sha256:worker"
            return f"{command[-1]} hash-{command[-1]}"
        if "ps" in command:
            return command[-1]
        if "exec" in command:
            return hashlib.sha256(("wrong" if drift == "key" else "synthetic-test-key").encode()).hexdigest()
        raise AssertionError(command)

    monkeypatch.setattr(target.subprocess, "check_output", output)
    url = "http://127.0.0.1:" + ("18080" if drift == "url" else "18081")
    if drift and drift != "pinned-image":
        with pytest.raises(ValueError):
            target.validate_target(state, url)
    else:
        _, settings, selected = target.validate_target(state, url)
        assert settings["BACKEND"] == backend and selected["worker"]["Id"] == "worker"
    assert all("pause" not in command and "restart" not in command for command in calls)
    if drift == "url":
        assert calls == []
