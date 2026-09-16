# 筆電日常使用

本服務可在 Windows 筆電使用，不以 Linux 桌機驗收為前提。Windows 驅動提供 GPU，Docker Desktop 使用 WSL2 backend 執行 Linux worker；平常可直接在 PowerShell 操作。

## 啟動及第一次呼叫

在 `D:\projects\selfhost-models` 開啟 PowerShell。先啟動 Docker Desktop，確認它已 ready：

```powershell
uv sync --locked
uv run --locked modelctl doctor
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend vllm --context 2048 --max-inflight 2 --gpu-memory 0.60
uv run --locked python scripts/chat.py "請用繁體中文簡短介紹你能協助的工作。"
uv run --locked python scripts/chat.py --stream "請列出三個整理工作筆記的方法。"
```

若尚未登錄，先 `uv run --locked modelctl register Qwen/Qwen3.5-4B --path D:/hf_models/Qwen3.5-4B`。不要對運行中的部署再執行 serve；已有服務時直接呼叫或查 status。

client 預設等待 ready 最多 300 秒；`--wait` 可調整。模型冷啟動與 kernel 編譯需要時間；逾時時檢查 worker logs，不無限重試生成。`--max-tokens` 預設 256，服務上限 1024，輸入加最大輸出必須在部署 context 內。

## API 接入

- Base URL：`http://127.0.0.1:18080/v1`。
- Model：`Qwen/Qwen3.5-4B`。
- API key：讀取本機 `.state/api-key`，不要複製到 Git 或日誌。
- 健康檢查：`http://127.0.0.1:18080/health/ready`。
- 使用 `Authorization: Bearer <key>`；範例與能力限制見 [API 文件](api.md)。

這是同一台筆電上的 HTTP 服務，未對 LAN 或公網開放。容器內的 `localhost` 指向該容器，不是 Windows 主機；目前未驗證其他容器或遠端裝置的接入，不能直接把 host 改成 0.0.0.0 當成正式遠端部署。

命令列 client 每次是單輪；多輪歷史由產品 client 自行提供。串流若缺 `[DONE]` 或收到 error frame，client 非零退出，已顯示的部分文字不代表成功。不會自動重送生成請求。

## 狀態、停機與切換

```powershell
uv run --locked modelctl status
uv run --locked modelctl stop
# 停止後，使用保存的配置重新啟動（不更換模型／backend）
uv run --locked modelctl restart
```

服務停止前先讓使用者請求完成。stop 保留權重、key 與 lease state。若 API 重啟後有未知工作，使用 modelctl restart 重啟本專案的 worker 與 API，等待新 epoch 暖機；不要刪除 journal。

切換到 Transformers：

```powershell
uv run --locked modelctl stop
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend transformers --context 2048 --max-inflight 1 --gpu-memory 0.60
uv run --locked python scripts/chat.py --stream "What is 2 plus 2? Reply with only the number."
```

切回 vLLM：stop 後重新執行上方 vLLM serve。一次只有一個 backend，Transformers 目前僅文字、單請求、queue 0；圖片／工具使用 vLLM。

## 重開機與常見狀況

- 目前沒有 OS 開機自動啟動或 Docker restart policy。Windows 重開機後，先等 Docker Desktop ready，再執行 `modelctl restart` 並等待 ready；本次不宣稱實際 Windows 重開機已驗證。
- 筆電需保持喚醒，Docker Desktop 需運行。休眠或關閉 Docker 期間 API 不可用。
- 429 表示容量滿，呼叫端可稍後發出新的請求；不要無界重試。504／斷線不代表 GPU 已停，先查 ready／lease 狀態。
- 健康檢查失敗時用 `docker compose --env-file .state/compose.env logs --tail 80 api worker`；Transformers 要額外加入 `-f compose.yaml -f compose.transformers.yaml`。
- 本次 vLLM 首次建置後曾出現長時間 kernel JIT、client 等待 360 秒未 ready，並保留未完成的暖機 lease。若 `/health/ready` 的 `uncertain` 大於 0，不可刪除 journal 或只增加生成重試；保存日誌後使用 `modelctl restart` 讓 worker 與 API 進入新 epoch，再用 `scripts/chat.py --wait 600` 檢查。若再次失敗，停止使用並診斷，不宣稱 ready。單純 worker 還在載入而沒有未決 lease 時，可延長 client 的等待。
- GPU 由桌面與模型共享；stop 可釋放模型常駐記憶體。不要為模型服務關閉其他程式。

## 自行驗收

可先執行 `uv run --locked pytest -q`。模型 ready 後，以新的證據目錄執行 [驗收文件](acceptance.md) 的一般、串流與 backend smoke。`--faults` 與串流生命週期腳本會 pause/restart 服務，必須安排無使用者請求的時段；不要在日常使用中執行。

Linux 桌機的完整步驟見 [桌機實機驗收](acceptance.md#linux-桌機實機驗收待執行)。
