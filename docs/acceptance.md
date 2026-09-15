# 驗收方式

## 契約與單元測試（不需 GPU）

```bash
python -m pytest -q
```

包含 registry 不改動權重、固定 revision／混合 revision 拒絕、缺 shard、檔案變更、錯誤欄位、API 驗證、deadline／斷線 lease、API 重啟隔離、SSE terminal／截斷／慢速消費者、inline image 與 tool history。

## 真實 GPU 服務

1. `modelctl doctor` 唯讀盤點 host 與共用資源。
2. register／fetch 選定模型，再 serve。不要停止其他專案服務。
3. 輪詢 `/health/ready` 到 200。
4. 執行：

```bash
python scripts/acceptance.py --faults --output evidence/acceptance.json
python scripts/multimodal_smoke.py --output evidence/multimodal.json
python scripts/stream_lifecycle_check.py
python scripts/capture_runtime.py
```

使用合成、非敏感文字；驗證模型列表、一般／SSE、錯誤參數、未知模型、context、認證、body 上限。`--faults` 只對經 Compose label 核對的 selfhost-models worker 做 pause/unpause，確認 429、deadline 504、斷線 lease 不提前釋放；再重啟 API 驗證未決工作保留，重啟 worker 驗證新 epoch 暖機恢復。finally 會 unpause worker，不刻意耗盡 GPU。

測試成功只表示服務協定與受控生命週期成功，不是產品效能、agent 成功率或領域模型品質。若任何 assertion 失敗，命令非零退出，不得把部分結果當全部通過。

## 主機差異

相同 Python／Compose 命令可在 Linux 重跑，但需先配置 NVIDIA Container Toolkit 及 Linux 的模型 path。Windows WSL2 成功不表示 Linux 桌機已驗證。以每次 JSON evidence 的時間、model revision、host/runtime 紀錄識別結果。

## 紀錄

本次驗收模型為 `Qwen/Qwen3.5-4B`，固定 revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`；唯讀接入 `D:/hf_models/Qwen3.5-4B`。Host 為 Windows Docker Desktop WSL2、RTX 5090 Laptop 24463 MiB、driver 581.57。

設定：BF16、context 2048、最多 2 個工作、等待佇列 0、GPU memory utilization 0.60、client deadline 30 秒、drain 上限 120 秒。可重現的啟動命令：

```bash
modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
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

最終快照 ready=true、inflight/detached/uncertain 皆 0；整張 GPU 使用 18805 MiB、空閒 5247 MiB。這包含桌面與 vLLM 常駐權重／cache，不是純權重大小。請求完成會釋放 admission 名額；`modelctl stop` 才停止本專案容器並歸還其常駐 GPU 記憶體。

fetch 的固定 revision 行為有契約測試，尚未實際下載外部模型。工具 smoke 不代表長程 agent 成功率；本版不提供音訊／影片 API、任意 URL 圖片或產品工具執行。
