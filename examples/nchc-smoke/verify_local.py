"""Bounded local verification of repository-owned fixed sample bytes only."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from make_sample import NUMBERS, PROGRAM, job_script


def run(argv, **kwargs):
    return subprocess.run(argv, capture_output=True, text=True, timeout=5, **kwargs)


with tempfile.TemporaryDirectory(prefix="nchc-smoke-local-") as temp:
    root = Path(temp)
    sample = root / "smoke.py"
    sample.write_text(PROGRAM)
    numbers, output = root / "numbers.txt", root / "result.json"
    numbers.write_text(NUMBERS)
    command = [sys.executable, "-I", str(sample), str(numbers), str(output)]
    clean_env = {"PATH": os.defpath, "CUDA_VISIBLE_DEVICES": ""}
    success = run(command, env=clean_env)
    assert success.returncode == 0, success.stderr
    result = json.loads(output.read_text())
    assert result == {"sum": 55, "input_count": 10, "gpu_computation": False}
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert run(command, env=clean_env).returncode != 0
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
    numbers.write_text("1\n2\n")
    wrong = run(command[:-1] + [str(root / "wrong.json")], env=clean_env)
    assert wrong.returncode != 0 and not (root / "wrong.json").exists()
    missing = run([sys.executable, "-I", str(sample), str(root / "missing.txt"), str(root / "missing.json")], env=clean_env)
    assert missing.returncode != 0 and not (root / "missing.json").exists()
    helper = Path(__file__).with_name("make_sample.py")
    assert run([sys.executable, str(helper)]).returncode != 0
    refused = run([sys.executable, str(helper), "--account", "PROJECT_ID", "--python", sys.executable,
                   "--work-root", "/work/UNVERIFIED", "--wallet-confirmed"])
    assert refused.returncode != 0 and "占位值" in refused.stderr
    unconfirmed = run([sys.executable, str(helper), "--account", "LOCALTEST0001", "--python", sys.executable,
                       "--work-root", "/work/UNVERIFIED"])
    assert unconfirmed.returncode != 0 and "wallet" in unconfirmed.stderr
    script = root / "job.slurm"
    # Synthetic account/path only for syntax; never submitted or written under /work.
    script.write_text(job_script("ACD114235", sys.executable, "/work/UNVERIFIED/nchc-smoke-local"))
    assert "#SBATCH --account=ACD114235\n" in script.read_text()
    assert run(["bash", "-n", str(script)]).returncode == 0
    guard = run(["bash", str(script)], env=clean_env)
    assert guard.returncode == 64 and "Only run via" in guard.stderr
    receipt = {"scope": "local_fixed_sample_only", "python": sys.version.split()[0],
               "sum": result["sum"], "result_sha256": digest, "json_readable": True,
               "overwrite_refused_and_bytes_preserved": True, "wrong_and_missing_input_nonzero": True,
               "missing_account_placeholder_unconfirmed_wallet_refused": True,
               "wallet_verified_account_accepted": True,
               "bash_syntax_passed": True, "non_slurm_execution_refused": True,
               "remote_connected": False, "submitted": False, "gpu_computation": False}
    target = Path(__file__).with_name("local-receipt.json")
    with target.open("x", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)
        f.write("\n")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
