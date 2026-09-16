"""Retain bounded inventory evidence; verify cleanup without stopping anything."""
import argparse
import json
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from selfhost_models.assets import Assets

ROOT = Path(__file__).resolve().parents[1]


def output(args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def main(args):
    containers = []
    ids = output(["docker", "ps", "-aq"]).splitlines()
    for identifier in ids:
        item = json.loads(output(["docker", "inspect", identifier]))[0]
        project = item["Config"]["Labels"].get("com.docker.compose.project") if item["Config"].get("Labels") else None
        if item["State"]["Running"] or project == "selfhost-models":
            containers.append({"id": item["Id"], "name": item["Name"], "project": project,
                "image_id": item["Image"], "started_at": item["State"]["StartedAt"],
                "running": item["State"]["Running"], "paused": item["State"]["Paused"],
                "exit_code": item["State"]["ExitCode"]})
    with socket.socket() as sock:
        port_open = sock.connect_ex(("127.0.0.1", args.port)) == 0
    record = Assets(args.state).inspect("Qwen/Qwen3.5-4B")
    snapshot = {"at": datetime.now(timezone.utc).isoformat(), "containers": containers,
        "gpu": output(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,memory.free", "--format=csv"]),
        "port": args.port, "port_open": port_open, "model_revision": record["revision"], "model_files": record["files"],
        "runtime_state_exists": (args.state / "runtime/leases.json").exists()}
    if args.before:
        before = json.loads(args.before.read_text())
        assert snapshot["model_files"] == before["model_files"]
        protected = [c for c in before["containers"] if c["project"] != "selfhost-models" and c["running"]]
        for container in protected:
            assert container in containers, "unrelated running container changed"
        ours = [c for c in containers if c["project"] == "selfhost-models"]
        assert ours and all(not c["running"] and not c["paused"] and c["exit_code"] == 0 for c in ours)
        assert not port_open and snapshot["runtime_state_exists"]
        snapshot["cleanup_checks"] = {"model_unchanged": True, "unrelated_services_unchanged": True,
                                      "services_stopped_exit_zero": True, "port_closed": True, "state_preserved": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in snapshot.items() if k != "model_files"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=ROOT / ".state")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before", type=Path)
    main(parser.parse_args())
