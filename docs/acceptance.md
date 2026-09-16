# 驗收方式

## 最新交付複驗（2026-09-16）

**最新 uv 版 API 與固定 vLLM worker 已通過完整真實 GPU 複驗，驗收後兩個服務均正常停止（exit 0）。** 本次交付程式起點為 `cea1d4a3dfb26edf2bf1b5a68bdc3dbac33dcb1d`；未修改 serving 程式、模型、Compose 設定或官方 worker 配套，僅補強證據腳本的輸出路徑及容器來源比對。

- 模型：Qwen/Qwen3.5-4B，revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`。Windows 11／Docker Desktop WSL2、RTX 5090 Laptop、driver 581.57。
- API：Python 3.12.14、uv 管理的 `/app/.venv`，正式環境不含 pytest；容器內 API 原始碼、pyproject 與 uv.lock 的 SHA256 與 checkout 全部相符。
- Worker：vLLM 0.29.0、torch 2.13.0+cu130、CUDA 13.0、Transformers 5.16.1，顯式 V1 runner。新建 image 的完整 ID 見 runtime 證據，官方基底 digest 未變。
- 04:04–04:07 UTC 完成全部驗收；先前停機原因調查另見本文末段，直接原因仍未確定。

| 驗證 | 結果／證據 |
|---|---|
| 基線單元／契約 | `uv lock --check --offline`、27 項 pytest 通過 |
| 一般／SSE、輸入／認證／接收上限 | 通過，[API 驗收](../evidence/2026-09-16/acceptance.json) |
| 超載、deadline、取消、API／worker 重啟 | 6×429 + 2×504；保留未完成 lease、完成後歸零；API 重啟隔離未決工作；worker 不可用回 503，新 epoch 暖機後回 200 |
| 圖片與工具往返 | 合成紅色圖片辨識、工具參數及結果 731 通過，[多模態驗收](../evidence/2026-09-16/multimodal.json) |
| SSE 已回 200 後的逾時／斷線 | error frame／無 DONE、保留工作到 terminal 後歸零，[串流生命週期](../evidence/2026-09-16/stream-lifecycle.json) |
| 版本與來源 | API 原始碼／鎖檔相符；ready=true、inflight/detached/uncertain=0，[Runtime](../evidence/2026-09-16/runtime.json) |
| 驗收後停機 | API／worker exit 0、未 paused、port 18080 關閉；模型 inventory 未變、state 保留；其他服務未重啟，[最終狀態](../evidence/2026-09-16/final-state.json) |

本次使用以下命令（先盤點共用資源；服務啟動後須完成整段並停機）：

```bash
uv run --locked modelctl doctor
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
uv run --locked modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
uv run --locked python scripts/acceptance.py --faults --output evidence/2026-09-16/acceptance.json
uv run --locked python scripts/multimodal_smoke.py --output evidence/2026-09-16/multimodal.json
uv run --locked python scripts/stream_lifecycle_check.py --output evidence/2026-09-16/stream-lifecycle.json
uv run --locked python scripts/capture_runtime.py --output evidence/2026-09-16/runtime.json
uv run --locked modelctl stop
```

原始啟動前／停機後 inspect 與 logs 保存在 `evidence/raw/2026-09-16-before-start/`、`evidence/raw/2026-09-16-after-stop/`，不提交 Git；摘要保留雜湊供本機核對。這次仍不代表 Linux 實體桌機、外部 HF 下載或產品級 agent 品質已驗證。

## 契約與單元測試（不需 GPU）

```bash
uv run --locked pytest -q
```

包含 registry 不改動權重、固定 revision／混合 revision 拒絕、缺 shard、檔案變更、錯誤欄位、API 驗證、deadline／斷線 lease、API 重啟隔離、SSE terminal／截斷／慢速消費者、inline image 與 tool history。

## 真實 GPU 服務

1. `uv run --locked modelctl doctor` 唯讀盤點 host 與共用資源。
2. register／fetch 選定模型，再 serve。不要停止其他專案服務。
3. 輪詢 `/health/ready` 到 200。
4. 執行：

```bash
uv run --locked python scripts/acceptance.py --faults --output evidence/acceptance.json
uv run --locked python scripts/multimodal_smoke.py --output evidence/multimodal.json
uv run --locked python scripts/stream_lifecycle_check.py
uv run --locked python scripts/capture_runtime.py
```

使用合成、非敏感文字；驗證模型列表、一般／SSE、錯誤參數、未知模型、context、認證、body 上限。`--faults` 只對經 Compose label 核對的 selfhost-models worker 做 pause/unpause，確認 429、deadline 504、斷線 lease 不提前釋放；再重啟 API 驗證未決工作保留，重啟 worker 驗證新 epoch 暖機恢復。finally 會 unpause worker，不刻意耗盡 GPU。

四個腳本皆可使用 `--output <path>`，複驗時使用新的日期目錄保留舊證據。`capture_runtime.py` 會比對容器內 API 原始碼／鎖檔與目前 checkout 的 SHA256，不一致時非零退出，不能拿舊 image 的結果代表新版本。

測試成功只表示服務協定與受控生命週期成功，不是產品效能、agent 成功率或領域模型品質。若任何 assertion 失敗，命令非零退出，不得把部分結果當全部通過。

## 主機差異

相同 Python／Compose 命令可在 Linux 重跑，但需先配置 NVIDIA Container Toolkit 及 Linux 的模型 path。Windows WSL2 成功不表示 Linux 桌機已驗證。以每次 JSON evidence 的時間、model revision、host/runtime 紀錄識別結果。

## 首次 GPU 驗收紀錄（2026-09-15）

本次驗收模型為 `Qwen/Qwen3.5-4B`，固定 revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`；唯讀接入 `D:/hf_models/Qwen3.5-4B`。Host 為 Windows Docker Desktop WSL2、RTX 5090 Laptop 24463 MiB、driver 581.57。

設定：BF16、context 2048、最多 2 個工作、等待佇列 0、GPU memory utilization 0.60、client deadline 30 秒、drain 上限 120 秒。可重現的啟動命令：

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
```

本次 JSON 證據：

- [API 與受控故障](../evidence/acceptance.json)：2026-09-15 09:27–09:29 UTC。
- [合成圖片與工具往返](../evidence/multimodal.json)：09:30 UTC。
- [SSE deadline／disconnect](../evidence/stream-lifecycle.json)：09:30 UTC。
- [Runtime、隔離與最終 readiness](../evidence/runtime.json)：包含 image ID、來源檔 SHA256、GPU 與固定模型 revision。`git_head` 是擷取當下的前一提交；`git_status` 明示尚未提交的驗收修正，來源 hash 用於核對本次實際程式。
- [選模前的歷史檢查](../evidence/pre-model-validation.json)：保留初期狀態，不代表最新驗收結果。

| 層級 | 2026-09-15 結果 |
|---|---|
| 單元／契約 | 27 passed |
| 實際 API container | loopback 連入、401、worker 缺席 503、持久化 fsync 通過；測試容器已移除 |
| 官方 worker runtime CUDA | 0.29.0 / torch 2.13.0+cu130 / CUDA 13.0 / transformers 5.16.1；八元素 tensor 計算通過 |
| 既有模型 register／inspect | Qwen3.5-4B 一致 revision、11 個必要檔案，未改動權重 |
| 真實模型 GPU 推論／完整故障驗收 | 通過一般／SSE、400／401／404／413、受控超載 6×429 + 2×504；保留 2 個未完成 lease，恢復後歸零 |
| 取消、API／worker 重啟 | 斷線保留 lease；API 重啟後未決工作使 ready=false；worker 不可用回 503；新 epoch 暖機後恢復，所有 lease 歸零 |
| 已送出 SSE 200 後的 deadline／disconnect | 通過；deadline 回 error frame 且不送 DONE；兩者都持續追蹤 GPU 工作到完成 |
| 真實 visual／tool roundtrip | 合成紅色 PNG 辨識為 Red；工具參數合法、由測試 client 執行合成查詢，模型接收結果並回答 731 |
| Linux 實體桌機 | **尚未驗證** |

實測修正：WSL2 的 V2 runner 因 UVA 不可用，明確改用同一官方 runtime 的 V1 runner。單 token 暖機未涵蓋 decode 編譯，首個正式請求曾逾時；暖機改為多 token、併發及合成視覺後，重新啟動並完成上述整套驗收。首次或新輸入 shape 的編譯時間仍可能不同。

最終快照 ready=true、inflight/detached/uncertain 皆 0；整張 GPU 使用 18805 MiB、空閒 5247 MiB。這包含桌面與 vLLM 常駐權重／cache，不是純權重大小。請求完成會釋放 admission 名額；`uv run --locked modelctl stop` 才停止本專案容器並歸還其常駐 GPU 記憶體。

fetch 的固定 revision 行為有契約測試，尚未實際下載外部模型。工具 smoke 不代表長程 agent 成功率；本版不提供音訊／影片 API、任意 URL 圖片或產品工具執行。

## uv 遷移驗證（2026-09-16）

此輪只變更 Python 套件管理與 API image 的安裝方式；Docker／Compose 管理容器，vLLM worker 的 CUDA／PyTorch 配套未變。上方 2026-09-15 GPU 證據保留原樣，本輪未啟動 GPU worker 或重跑 GPU 驗收。

uv 0.12.15 + Python 3.12.14：舊 `requirements.lock` 的 30 個套件版本與 `uv.lock` 逐一比對無差異。PATH 既有 uv 0.11.21 也通過同一鎖檔的離線 sync、27 項測試與 CLI 檢查；因此專案支援 >=0.11.21,<0.13，Docker 工具固定 0.12.15。Windows 安裝依上游 metadata 排除 hf-xet，Linux API image 安裝 hf-xet 1.6.0。

| 指令／檢查 | 結果 |
|---|---|
| `uv sync --locked` | 新建 `.venv` 並安裝成功 |
| `uv lock --check --offline` | 鎖檔與專案一致 |
| `uv run --locked pytest -q` | 27 passed |
| `uv pip check` | 30 個已安裝套件相容 |
| `uv run --locked modelctl --help` | CLI entry point 正常 |
| `docker build -f docker/api.Dockerfile -t selfhost-models-api:0.1.0 .` | 成功，兩段 `uv sync --locked --no-dev` |
| `uv run --locked python scripts/api_container_check.py` | loopback、401、worker 缺席 503、持久化 fsync 通過；測試容器已移除 |
| 新 image 的 `--network none --read-only` Python import | 成功，`sys.prefix=/app/.venv`，pytest 不存在 |

新 API image ID（`docker image inspect`）：`sha256:62f1c05a6bdcfcd37677f43f422d8134e596364b863bfeba212fb94cad73d22b`。此 Linux container 建置／執行證據來自 Windows Docker Desktop，不等於 Linux 實體桌機驗證。

## Exited (255) 調查（2026-09-16，啟動前）

已先保存兩個舊容器的完整 inspect／logs，再執行任何啟動或重建。可提交的 [調查摘要與原始檔 SHA256](../evidence/2026-09-16/exit-investigation.json) 指向本機 `evidence/raw/2026-09-16-before-start/`；原始 inspect、完整日誌及主機事件留在 Git 忽略目錄，避免將主機細節混入公開成果。

已確認：

- API／worker 均為 exit 255、`OOMKilled=false`、`Error` 空字串、restart policy `no`；啟動前工作 lease 為空。
- Windows 系統事件 1074、13、12 記錄 9/15 18:29–18:31（台北時間）由系統更新觸發的計畫重啟；API 最後健康日誌為 18:29:17。
- Docker Desktop 在 9/16 09:59 啟動 daemon、載入容器；兩個舊容器 `FinishedAt` 同為 09:59:19，該時段還有它們的 `layer not mounted` 與 `Removing stale sandbox` 紀錄。
- Docker event 查詢未保留對應的原始退出事件；應用程式日誌没有該結束時間的直接退出原因。

**255 的直接原因仍無法確定。** 上述時序不能證明是應用程式崩潰、CUDA OOM 或特定 Docker bug，也不能將 daemon 恢復時寫下的時間直接當成實際程序終止時間。本次不據此更換 runtime 或加入自動重啟政策。
