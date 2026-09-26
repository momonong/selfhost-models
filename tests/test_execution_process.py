"""Real loopback protocol with mutually filesystem-isolated CPU processes."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "executor_process.py"


async def launch(config):
    env = {**os.environ, "PYTHONPATH": str(FIXTURE.resolve().parents[2]), "PYTHONDONTWRITEBYTECODE": "1"}
    process = await asyncio.create_subprocess_exec(sys.executable, str(FIXTURE),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    process.stdin.write(json.dumps(config).encode() + b"\n")
    await process.stdin.drain()
    process.stdin.close()
    return process


async def stop(process):
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        process.kill()
        await process.wait()


@pytest.mark.skipif(sys.platform != "linux", reason="Landlock OS isolation requires Linux")
async def test_real_http_control_executor_are_mutually_filesystem_isolated(tmp_path):
    control_root, executor_root = tmp_path / "control", tmp_path / "executor"
    control_root.mkdir()
    weights = executor_root / "weights" / "fixture-model"
    weights.mkdir(parents=True)
    (weights / "config.json").write_text("{}")
    (weights / "model.safetensors").write_bytes(b"cpu-fixture-only")
    # Existing files make denied-read evidence stronger than a missing-path check.
    (control_root / "scheduler.sqlite3").touch()
    (control_root / "artifacts").mkdir()
    private_artifact = control_root / "artifacts" / "private-audio.canary"
    private_artifact.write_bytes(b"must not be readable by executor")
    key = "synthetic-process-key-" + "x" * 40
    executor = control = None
    try:
        executor = await launch({"role": "executor", "root": str(executor_root), "key": key, "forbidden": [
            {"path": str(control_root / "scheduler.sqlite3"), "label": "control_sqlite", "directory": False},
            {"path": str(private_artifact), "label": "control_artifact", "directory": False},
            {"path": str(control_root), "label": "control_state_directory", "directory": True}]})
        line = await asyncio.wait_for(executor.stdout.readline(), 15)
        if not line:
            assert False, (await executor.stderr.read()).decode()
        ready = json.loads(line)
        if "unsupported" in ready:
            pytest.skip(ready["unsupported"])
        assert ready["ready"] and ready["url"].startswith("http://127.0.0.1:")
        control = await launch({"role": "control", "root": str(control_root), "key": key, "executor": ready,
            "forbidden": [
                {"path": str(executor_root / "executor.sqlite3"), "label": "executor_journal", "directory": False},
                {"path": str(weights / "model.safetensors"), "label": "executor_weights", "directory": False},
                {"path": str(executor_root / "weights"), "label": "executor_weights_parent", "directory": True}]})
        stdout, stderr = await asyncio.wait_for(control.communicate(), 45)
        assert control.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        if "unsupported" in result:
            pytest.skip(result["unsupported"])
        assert executor.returncode is None
        evidence = [ready["isolation"], result["isolation"]]
        assert {entry["pid"] for entry in evidence} == {executor.pid, control.pid}
        assert all(entry["mechanism"] == "landlock" and entry["abi"] >= 1 for entry in evidence)
        assert {item["label"] for entry in evidence for item in entry["denied"]} == {
            "control_sqlite", "control_artifact", "control_state_directory",
            "executor_journal", "executor_weights", "executor_weights_parent"}
        assert all(item["error"] == "PermissionError" for entry in evidence for item in entry["denied"])
        assert result["qwen"]["result"]["choices"][0]["finish_reason"] == "stop"
        assert result["whisper"]["result"]["terminal"] is True
        assert result["sse_done"] and result["detached_lease_held"]
        assert result["control_phase"] == "unloaded" and result["leases"] == 0 and result["pending"] == []
        calls = result["executor"]["calls"]
        assert [call["model"] for call in calls if call["kind"] == "load"] == ["Qwen/Qwen3.5-4B", "openai/whisper-small"]
        assert len([call for call in calls if call["kind"] == "prepare"]) == 2
        assert len([call for call in calls if call["kind"] == "warmup"]) == 2
        generations = [call for call in calls if call["kind"] in ("execute", "stream")]
        assert len(generations) == len({call["attempt"] for call in generations}) == 4
        commands = {item["id"]: item for item in result["executor"]["commands"]}
        assert all(commands[call["attempt"]]["acknowledged"] for call in generations)
        assert next(call["audio_sha256"] for call in generations if "audio_sha256" in call) == result["whisper"]["result"]["audio_sha256"]
        (tmp_path / "isolation-evidence.json").write_text(json.dumps(result, indent=2) + "\n")
    finally:
        await stop(control)
        await stop(executor)
