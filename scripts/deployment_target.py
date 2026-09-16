"""Fail closed before evidence or faults can target a different deployment."""
import json
import hashlib
import subprocess
from urllib.parse import urlsplit

from selfhost_models.cli import compose_command


def validate_target(state, url):
    state = state.resolve()
    settings = {k: v.strip().strip("'\"") for k, v in
                (line.split("=", 1) for line in (state / "compose.env").read_text().splitlines()
                 if line and not line.startswith("#"))}
    endpoint = urlsplit(url)
    if (endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost")
            or endpoint.username or endpoint.password or endpoint.path not in ("", "/")
            or endpoint.query or endpoint.fragment
            or (endpoint.port or 80) != int(settings["API_PORT"])):
        raise ValueError("URL must identify the configured loopback API port")

    def output(args):
        return subprocess.check_output(args, text=True, encoding="utf-8", timeout=30).strip()

    compose = compose_command(state)
    config = json.loads(output([*compose, "config", "--format", "json"]))
    containers = {}
    for service in ("api", "worker"):
        identifier = output([*compose, "ps", "-q", service])
        if not identifier or "\n" in identifier:
            raise ValueError("expected one running container per service")
        obj = json.loads(output(["docker", "inspect", identifier]))[0]
        labels = obj["Config"].get("Labels", {})
        if (labels.get("com.docker.compose.project") != "selfhost-models"
                or labels.get("com.docker.compose.service") != service
                or not obj["State"]["Running"] or obj["State"]["Paused"]):
            raise ValueError("container is not the running project service")
        expected = config["services"][service]
        actual_env = dict(value.split("=", 1) for value in obj["Config"]["Env"])
        for key, value in expected.get("environment", {}).items():
            if str(actual_env.get(key)) != str(value):
                raise ValueError(f"{service} configuration mismatch: {key}")
        if obj["Config"]["Image"] != expected["image"]:
            raise ValueError(f"{service} image/backend mismatch")
        containers[service] = obj
    api = containers["api"]
    # Compose's service hash includes bind paths, secrets, backend and settings.
    # This also detects a copied key paired with a different runtime directory.
    for service, obj in containers.items():
        expected_hash = output([*compose, "config", "--hash", service]).split()[-1]
        if obj["Config"]["Labels"].get("com.docker.compose.config-hash") != expected_hash:
            raise ValueError(f"{service} deployment configuration differs from state")
    ports = api["NetworkSettings"]["Ports"].get("8000/tcp") or []
    if ports != [{"HostIp": "127.0.0.1", "HostPort": settings["API_PORT"]}]:
        raise ValueError("API published port does not match URL/state")
    # Compare bytes inside the selected API, without returning the secret.
    key = (state / "api-key").read_text().strip()
    deployed_key = output([*compose, "exec", "-T", "api", "python", "-c",
                           "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('/run/secrets/api_key').read_text().strip().encode()).hexdigest())"])
    if not key or hashlib.sha256(key.encode()).hexdigest() != deployed_key:
        raise ValueError("state authentication does not match selected API")
    return compose, settings, containers
