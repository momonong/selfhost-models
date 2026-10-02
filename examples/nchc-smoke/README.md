# Nano5 最小測試：1 到 10 的總和

這個樣本把 `numbers.txt` 的 1–10 加總，寫出 `result.json`，答案應為 **55**。它只測試檔案、Python、Slurm 與結果下載流程。腳本申請 dev 的 1 GPU，但程式只用 CPU，不能視為 GPU 運算驗收；實際執行可能產生少量費用，時間上限兩分鐘。

2026-10-02 使用者在 Nano5 終端的查詢、經 main 轉交：`PROJECT_ID=ACD114235`、Python 為 `/usr/bin/python3`、工作目錄 `/work/momonong0512`（owner 可寫）。餘額每次提交前自行查 wallet，不公開歷史餘額；遠端 Python 版本未記錄，固定標準函式庫樣本已在本次 Slurm 計算節點完成，研究環境仍未驗證。產生器與程式使用 Python 3.6+ 標準函式庫。

2026-10-03 使用者完成 Nano5 手動流程 smoke：Job 369175 成功、結果 55 並下載核對，見 [測試紀錄與證據來源](../../docs/nchc-personal.md#nano5-手動測試紀錄)。這不代表 GPU 運算、研究環境、所有 OS 實機或 UI 真實提交驗收通過；以下保留手動操作方式。

本機可用 Ubuntu 終端、macOS Terminal 或 Windows PowerShell／Terminal，前提是有 `ssh`／`sftp` 客戶端；尚未具備時先準備客戶端。本機只負責登入與下載，不需要 Python／uv，也不用在每台裝置安裝 repo。上述帳號、account、Python 與 `/work` 路徑是個人遠端設定，不依本機 OS 換成本機 home。

## 1. 在已登入 Nano5 的遠端 Bash 終端建立樣本

先執行 `wallet ACD114235`。依[官方 wallet 說明](https://man.twcc.ai/@AI-Pilot/rygXKNuNMyg)，必須是有效計畫且 `SU_BALANCE` 大於零。若結果不符，先停下。不要從其他代號猜 account，也不要自動挑第一個計畫。

符合後，把下面**整段**貼進已登入 Nano5 的遠端 Bash 終端，勿貼入本機 PowerShell。遠端 shell 若不是 Bash，先在遠端執行 `bash` 再貼。所有 `read -p`、`[[ ]]` 與 heredoc／Python 生成器均在遠端執行。在提示中輸入 `yes`，表示你剛核對有效計畫與正餘額。它只建立一個新的私有測試目錄及三個固定檔案，不會提交工作，不需要安裝 repo 或套件。

```bash
(
  read -r -p '已核對 ACD114235 有效且 SU_BALANCE > 0？輸入 yes：' NCHC_WALLET_OK
  [ "$NCHC_WALLET_OK" = yes ] || { echo '停止：請先核對 wallet。' >&2; exit 1; }
  [ -x /usr/bin/python3 ] || { echo '停止：已確認的 Python 不可執行。' >&2; exit 1; }
  /usr/bin/python3 - --account ACD114235 --python /usr/bin/python3 --work-root /work/momonong0512 --wallet-confirmed <<'NCHC_SMOKE_PY'
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
NCHC_SMOKE_PY
)
```

記下它印出的新目錄。若任何一步出錯，保留錯誤並停下，不載入 module、換環境或重試提交。

## 2. 在 Nano5 遠端 Bash 終端閱讀腳本，手動提交一次

```bash
read -r -p '貼上剛印出的完整測試目錄：' NCHC_SMOKE_DIR
cd -- "$NCHC_SMOKE_DIR" && cat job.slurm
```

核對 account 為 `ACD114235`，資源為 dev／1 node／1 task／1 CPU／1 GPU／2 GiB、最多 `00:02:00`，以及輸出目錄正確。確認願意使用這次資源後，由你執行下面一行**一次**，記下回覆的 Job ID：

```bash
sbatch "$NCHC_SMOKE_DIR/job.slurm"
```

不用 `bash job.slurm`；腳本會拒絕直接執行。每次測試都使用新目錄，既有 `result.json` 不會被覆寫。提交失敗或結果不明時，保留 Job ID 與日誌，先停止，不重送原工作。

## 3. 在 Nano5 遠端 Bash 終端確認結果

輸入真實 Job ID，查詢一次。尚未完成就稍候再查；不要因為仍在排隊而重送。

```bash
read -r -p '輸入 sbatch 回覆的 Job ID：' NCHC_JOB_ID
if [[ "$NCHC_JOB_ID" =~ ^[0-9]+$ ]]; then
  sacct -j "$NCHC_JOB_ID" --format=JobID,State,ExitCode
else
  echo '停止：Job ID 必須是數字。' >&2
fi
```

看到主工作與計算 step 為 `COMPLETED`、`ExitCode=0:0` 後，在測試目錄執行 `cat result.json`，應得到：

```json
{
  "sum": 55,
  "input_count": 10,
  "gpu_computation": false
}
```

查詢終態的方法參考[官方工作管理說明](https://man.twcc.ai/@AI-Pilot/r1os5G_Mkl)。失敗時閱讀同目錄的 `slurm-JobID.err`／`.out`（檔名中的 JobID 換成實際數字）；即使有結果檔，也仍須確認工作終態。

## 4. 在另一個本機終端下載

這一步在 **本機終端**操作，保留原本的 Nano5 SSH 視窗。下列 SFTP 單行命令可在 Ubuntu、macOS、Windows 的上述終端使用：

```bash
sftp -P 2222 momonong0512@nano5.nchc.org.tw
```

主機與 port 依[官方資料傳輸節點說明](https://man.twcc.ai/@AI-Pilot/SkDyJN4Gkl)；密碼／OTP 只在終端提示中自行輸入。連線後，在 `sftp>` 輸入 `cd` 加上剛才記下的完整測試目錄，再輸入：

```text
get result.json
bye
```

檔案會下載到當次裝置開啟 SFTP 時的本機目前目錄，可在 `sftp>` 輸入 `lpwd` 查看。登入或下載失敗時保留錯誤，不自動改用其他主機。

## 本機驗證紀錄

[local-receipt.json](local-receipt.json) 記錄本機 Python 3.12 的固定樣本驗證：總和 55、可讀 JSON、既有輸出拒絕覆寫且內容不變、錯誤／缺少輸入回傳非零、account 缺少／占位值與未確認 wallet 時拒絕、已核實 account 可產生腳本、`bash -n` 通過、非 Slurm 執行被拒。

這是保留原樣的維護者歷史本機驗證紀錄，使用上述樣本不需要在本機執行此驗證或安裝 Python／uv。該次驗證只在本機暫存目錄執行固定程式；沒有 Nano5 連線、工作提交或 GPU 運算，也沒有修改其他 UI、服務或專案依賴。維護者使用 repo 的 locked uv 執行 `python examples/nchc-smoke/verify_local.py`；紀錄以排他建立保存，既有紀錄不覆寫。後續由使用者完成的 Nano5 手動流程另記於上方連結。
