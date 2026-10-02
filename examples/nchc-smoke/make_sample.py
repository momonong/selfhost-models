"""Create only this fixed smoke sample. Never submit, connect, or install anything."""
import argparse
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile

NUMBERS = "1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n"
PROGRAM = '''import json
import sys

def main():
    if len(sys.argv) != 3:
        raise ValueError("Usage: smoke.py numbers.txt result.json")
    with open(sys.argv[1], "rb") as source:
        raw = source.read(1025)
    if len(raw) > 1024:
        raise ValueError("Input exceeds 1024 bytes")
    values = [int(line) for line in raw.decode("utf-8").splitlines()]
    if values != list(range(1, 11)):
        raise ValueError("Expected exactly the numbers 1 through 10")
    result = {"sum": sum(values), "input_count": len(values), "gpu_computation": False}
    payload = json.dumps(result, indent=2) + "\\n"
    with open(sys.argv[2], "x", encoding="utf-8") as output:
        output.write(payload)
    print(payload, end="")

try:
    main()
except (OSError, ValueError) as exc:
    print("Smoke failed: " + str(exc), file=sys.stderr)
    sys.exit(1)
'''


def account_value(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value or ""):
        raise ValueError("請填 wallet 已核可的 Slurm PROJECT_ID；沒有預設 account")
    if value.upper() in {"PROJECT_ID", "YOUR_ACCOUNT", "ACCOUNT", "CHANGE_ME", "NANO5_PROJECT_REQUIRED"}:
        raise ValueError("不可使用占位值作 Slurm account")
    return value


def absolute_path(value, work=False):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value or "") or any(p in ("", ".", "..") for p in value[1:].split("/")):
        raise ValueError("路徑需為已確認的絕對路徑，僅英數字、_、-、.、/，不可含空白或 . / ..")
    if work and not value.startswith("/work/"):
        raise ValueError("工作目錄必須位於 /work 下，且由使用者確認可寫")
    return value


def job_script(account, python, directory):
    account = account_value(account)
    python = absolute_path(python)
    directory = absolute_path(directory, work=True)
    return f'''#!/bin/bash
#SBATCH --account={account}
#SBATCH --job-name=nchc-smoke
#SBATCH --partition=dev
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --gpus-per-node=1
#SBATCH --mem=2G
#SBATCH --time=00:02:00
#SBATCH --no-requeue
#SBATCH --chdir={directory}
#SBATCH --output={directory}/slurm-%j.out
#SBATCH --error={directory}/slurm-%j.err
set -euo pipefail
[[ "${{SLURM_JOB_ID:-}}" =~ ^[0-9]+$ ]] || {{ echo "Only run via verified Slurm submission." >&2; exit 64; }}
cd -- {shlex.quote(directory)}
[[ -x {shlex.quote(python)} ]] || {{ echo "Verified Python unavailable on this node." >&2; exit 66; }}
[[ ! -e result.json && ! -L result.json ]] || {{ echo "Result already exists; refusing overwrite." >&2; exit 73; }}
srun --ntasks=1 --cpus-per-task=1 {shlex.quote(python)} -I smoke.py numbers.txt result.json
'''


def main():
    parser = argparse.ArgumentParser(description="只建立固定 55 測試，不提交國網")
    parser.add_argument("--account", required=True, help="wallet 顯示的核可 PROJECT_ID")
    parser.add_argument("--python", required=True, help="command -v python3 已確認的絕對路徑")
    parser.add_argument("--work-root", required=True, help="/work 下已確認可寫的目錄")
    parser.add_argument("--wallet-confirmed", action="store_true", help="你已確認有效計畫及 SU_BALANCE > 0")
    args = parser.parse_args()
    account = account_value(args.account)
    if not args.wallet_confirmed:
        raise ValueError("先自行核對 wallet 計畫有效且 SU_BALANCE > 0；不能自動挑第一個計畫")
    python = Path(absolute_path(args.python))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("已指定的 Python 不存在或不可執行；停止，不自動載入 module")
    root = Path(absolute_path(args.work_root, work=True)).resolve(strict=True)
    absolute_path(str(root), work=True)
    if not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise ValueError("工作目錄不可寫，請先核對 /work 路徑，不自動建立帳號目錄")
    directory = Path(tempfile.mkdtemp(prefix="nchc-smoke-", dir=root))
    for name, content in {"numbers.txt": NUMBERS, "smoke.py": PROGRAM,
                          "job.slurm": job_script(account, str(python), str(directory))}.items():
        with (directory / name).open("x", encoding="utf-8") as output:
            output.write(content)
        os.chmod(directory / name, 0o600)
    print("已建立 " + str(directory))
    print("尚未提交。請先閱讀 job.slurm，核對 account／資源／費用，再由你手動 sbatch 一次。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print("停止：" + str(exc), file=sys.stderr)
        sys.exit(1)
