# 我的國網操作筆記

日常使用本機終端：登入 Nano5、交給 Slurm 執行、查結果，再下載檔案。Ubuntu 終端、macOS Terminal、Windows PowerShell／Terminal 都可以，前提是有 `ssh`／`sftp` 客戶端；尚未具備時先準備客戶端。iService 用於帳號、認證／OTP 與計畫管理。目前以 CLI／手動實測優先，UI 保留為離線原型並停止擴充。

## 已確認的資料

以下來自 2026-10-02 使用者在 Nano5 的查詢，經 main 轉交：

| 項目 | 本次確認值 |
| --- | --- |
| 登入帳號 | `momonong0512` |
| wallet PROJECT_ID／Slurm account | `ACD114235` |
| 登入節點 Python 路徑 | `/usr/bin/python3` |
| 工作目錄 | `/work/momonong0512`，owner 可寫 |

帳號、account、`/usr/bin/python3` 與 `/work/momonong0512` 是個人遠端設定，不隨本機 OS 改成本機 home。遠端 Python 版本未記錄；固定標準函式庫樣本已在本次 Slurm 計算節點完成，研究環境仍未驗證。每次提交前仍須查 wallet，確認計畫有效且餘額為正。其他服務頁若列出台灣杉三號／創進一號，需另查其登入與排程規則，本筆記只處理 Nano5。

## 一次準備

1. 在 iService 確認帳號、認證方式與可用計畫。密碼／OTP 只輸入認證提示，不貼進筆記或聊天。
2. 研究程式繼續留在原研究 repo；需要執行的版本、輸入資料與環境再準備到 Nano5 的 `/work`。
3. 首次照 [55 最小測試](../examples/nchc-smoke/README.md) 建立樣本；本機不需要 Python／uv，也不用在每台裝置安裝這個 repo 或開 UI。

**本機終端**登入：下列 SSH／SFTP 單行命令在上述三種系統相同。Nano5 SSH 使用 port 22，資料傳輸使用 2222。目前已有 Nano5 SSH 視窗就直接繼續使用。

```bash
ssh -p 22 momonong0512@nano5.nchc.org.tw
```

登入後，以下 `wallet`、`read -p`、`[[ ]]`、提交／查詢及樣本 heredoc／Python 生成器都在 **Nano5 遠端 Bash 終端**執行，勿貼入本機 PowerShell。遠端 shell 若不是 Bash，先在遠端執行 `bash` 再貼。

## 每次 job：準備 → 提交 → 查結果

job 是「要多少資源、在哪裡執行、跑什麼命令」的描述；`.py` 是其中要執行的程式。`job.slurm` 指定 account、partition、GPU／CPU 數量、記憶體、時間、程式與日誌位置。選資源即可，節點由 Slurm 分配，不手挑 `hgpn` 節點。

**已登入 Nano5 的遠端 Bash 終端**先查：

```bash
wallet ACD114235
```

計畫有效且餘額為正，再為本次 job 準備新的 `/work` 目錄、程式、輸入與腳本。第一次使用上述 55 樣本：dev／1 GPU／1 CPU／2 GiB／最多兩分鐘；這是流程 smoke，程式只用 CPU，仍可能有少量費用。研究工作另依需求選資源與環境，不直接沿用樣本規格。

```bash
read -r -p '本次已準備的 /work 完整工作目錄：' NCHC_JOB_DIR
if [[ "$NCHC_JOB_DIR" == /work/* && -f "$NCHC_JOB_DIR/job.slurm" ]]; then
  cat "$NCHC_JOB_DIR/job.slurm"
else
  echo '停止：先確認工作目錄與腳本。' >&2
fi
```

閱讀並確認 account、資源、程式、輸入及輸出路徑正確後，**手動提交一次**，記下回覆的 Job ID：

```bash
sbatch "$NCHC_JOB_DIR/job.slurm"
```

只查一次；未完成就稍候再手動查，不開 watch 輪詢：

```bash
read -r -p 'sbatch 回覆的數字 Job ID：' NCHC_JOB_ID
if [[ "$NCHC_JOB_ID" =~ ^[0-9]+$ ]]; then
  sacct -j "$NCHC_JOB_ID" --format=JobID,State,ExitCode
else
  echo '停止：Job ID 必須是數字。' >&2
fi
```

| State | 接下來做什麼 |
| --- | --- |
| PENDING | 已排隊，等待資源，不重送 |
| RUNNING | 正在執行，等待完成 |
| COMPLETED | 核對主工作與計算 step 的 `ExitCode=0:0`，再看輸出 |
| FAILED | 保留 Job ID、`.out`／`.err`，先看失敗原因 |

其他終態如 TIMEOUT／CANCELLED 也不是成功。輸出檔存在不代表工作完成；提交回覆不明時先查紀錄，確認原工作狀態，不直接重送。運算交給 Slurm，登入節點不跑運算或常駐服務。

## 下載結果：本機終端

```bash
sftp -P 2222 momonong0512@nano5.nchc.org.tw
```

完成終端認證後，在 `sftp>` 輸入 `cd` 加上本次完整工作目錄，再用 `get result.json` 下載 55 樣本結果、`bye` 離開。研究輸出改用實際檔名；結果下載到當次使用的裝置，`lpwd` 可查本機下載位置。

官方參考：[使用注意事項與連線 port](https://man.twcc.ai/@AI-Pilot/SkxWj5GwY1g)、[wallet](https://man.twcc.ai/@AI-Pilot/rygXKNuNMyg)、[工作管理](https://man.twcc.ai/@AI-Pilot/r1os5G_Mkl)、[資料傳輸](https://man.twcc.ai/@AI-Pilot/SkDyJN4Gkl)。

## Nano5 手動測試紀錄

- 2026-10-03（臺灣）：使用者以 account `ACD114235` 手動提交，收到 `Submitted batch job 369175`。
- 遠端目錄：`/work/momonong0512/nchc-smoke-ksq8wg`；dev／1 CPU／1 GPU／2 GiB／兩分鐘／no-requeue。
- `sacct`：主工作 `369175`、batch、extern（原輸出截短為 `exte+`）、0 step 均為 `COMPLETED`、`ExitCode=0:0`。
- `result.json`：`sum=55`、`input_count=10`、`gpu_computation=false`。
- 使用者 SFTP 下載完成：65 bytes；[結果檔](../evidence/2026-10-03-nano5-smoke/nano5-369175-result.json) 已移入本 repo。最初下載於 `/home/ubuntu/projects/selfhost-servers/nano5-369175-result.json`（歷史位置，原檔已移走）。
- SHA256：`566774a5a7ebfeb173ee1401abdc257bdcea403bb0613fb7e0ac3d1d5722afa7`，與本機固定樣本預期一致。

證據來源：遠端提交、終態及下載進度由使用者貼回，main 轉交必要非秘密值；下載檔由 main 與協調者直接在本機核對 bytes、JSON、SHA256。代理沒有連線或提交國網工作。

[歷史本機固定樣本驗證](../examples/nchc-smoke/local-receipt.json) 保留原樣。本次 Nano5 手動流程 smoke 已完成；GPU 運算、研究環境、所有 OS 實機與 UI 真實提交均未驗收，UI 仍為離線原型。
