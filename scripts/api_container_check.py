"""Real Docker API check without any model worker or GPU allocation."""
import json
import os
import secrets
import socket
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".state/api-container-check"


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "runtime").mkdir(exist_ok=True)
    key = secrets.token_urlsafe(32)
    (STATE / "api-key").write_text(key)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    values = {"MODEL_ID": "contract/no-model", "MODEL_REVISION": "0" * 40,
        "API_UID": str(os.getuid() if os.name != "nt" else 10001),
        "API_GID": str(os.getgid() if os.name != "nt" else 10001),
        "MODEL_PATH": STATE.as_posix(), "API_KEY_PATH": (STATE / "api-key").as_posix(),
        "API_STATE_PATH": (STATE / "runtime").as_posix(), "API_PORT": str(port)}
    env_file = STATE / "check.env"
    env_file.write_text("".join(f"{k}='{v}'\n" for k, v in values.items()))
    command = ["docker", "compose", "-p", "selfhost-models-api-check", "--env-file", str(env_file),
               "-f", str(ROOT / "compose.yaml")]
    subprocess.run([*command, "config", "-q"], check=True)
    try:
        subprocess.run([*command, "up", "-d", "--no-deps", "api"], check=True)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=3) as client:
            until = time.monotonic() + 30
            while time.monotonic() < until:
                try:
                    if client.get("/health/live").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            r = client.get("/health/ready")
            assert r.status_code == 503 and r.json()["ready"] is False
            assert client.get("/v1/models").status_code == 401
            client.headers["Authorization"] = "Bearer " + key
            assert client.get("/v1/models").status_code == 503
            r = client.post("/v1/chat/completions", json={"model": "contract/no-model", "messages": [{"role": "user", "content": "synthetic"}]})
            assert r.status_code == 503
            assert "synthetic" not in r.text
        subprocess.run([*command, "exec", "-T", "api", "python", "-c",
            "from pathlib import Path; from selfhost_models.storage import atomic_json; atomic_json(Path('/state/probe.json'), {'fsync': True})"], check=True)
        assert json.loads((STATE / "runtime/probe.json").read_text()) == {"fsync": True}
        print(json.dumps({"api_container": "passed", "health": 503, "authentication": 401,
                          "worker_missing": 503, "persistent_fsync": True, "model_loaded": False}))
    finally:
        subprocess.run([*command, "down"], check=True)


if __name__ == "__main__":
    main()
