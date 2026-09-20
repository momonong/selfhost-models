"""Daemon-wide ownership shared by Windows static and Linux managed modes.

Named-volume create is atomic/idempotent; existing labels remain unchanged.
No data is written to this volume. Heartbeats never expire ownership.
"""
import json
import secrets
import subprocess
from pathlib import Path

from .scheduler_schema import SchedulerError
from .scheduler_store import durable_write


class GPUOwnership:
    def __init__(self, state, mode, *, name="selfhost-gpu-0-owner", run=None):
        self.state, self.mode, self.name = Path(state), mode, name
        self.run = run or self._run
        self.token_path = self.state / "gpu-owner-token"

    @staticmethod
    def _run(args):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise SchedulerError("gpu_ownership_unavailable", 503)
        return result.stdout.decode()

    def token(self):
        self.state.mkdir(parents=True, exist_ok=True)
        if not self.token_path.exists():
            durable_write(self.token_path, secrets.token_hex(32).encode())
            self.token_path.chmod(0o600)
        return self.token_path.read_text().strip()

    def acquire(self):
        token = self.token()
        self.run(["volume", "create", "--label", "selfhost.owner=" + token,
                  "--label", "selfhost.mode=" + self.mode, self.name])
        info = json.loads(self.run(["volume", "inspect", self.name]))
        if len(info) != 1 or info[0].get("Labels", {}).get("selfhost.owner") != token or info[0].get("Labels", {}).get("selfhost.mode") != self.mode:
            raise SchedulerError("gpu_owner_conflict", 409)
        return self

    def release(self):
        info = json.loads(self.run(["volume", "inspect", self.name]))
        if len(info) != 1 or info[0].get("Labels", {}).get("selfhost.owner") != self.token():
            raise SchedulerError("gpu_owner_conflict", 409)
        ids = self.run(["ps", "-q"]).split()
        if ids:
            containers = json.loads(self.run(["inspect", *ids]))
            if any(c.get("HostConfig", {}).get("DeviceRequests") or c.get("HostConfig", {}).get("Devices") for c in containers):
                raise SchedulerError("gpu_exit_unconfirmed", 409)
        self.run(["volume", "rm", self.name])
