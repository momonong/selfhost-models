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
```

使用合成、非敏感文字；驗證模型列表、一般／SSE、錯誤參數、未知模型、context、認證、body 上限。`--faults` 只對經 Compose label 核對的 selfhost-models worker 做 pause/unpause，確認 429、deadline 504、斷線 lease 不提前釋放；再重啟 API 驗證未決工作保留，重啟 worker 驗證新 epoch 暖機恢復。finally 會 unpause worker，不刻意耗盡 GPU。

測試成功只表示服務協定與受控生命週期成功，不是產品效能、agent 成功率或領域模型品質。若任何 assertion 失敗，命令非零退出，不得把部分結果當全部通過。

## 主機差異

相同 Python／Compose 命令可在 Linux 重跑，但需先配置 NVIDIA Container Toolkit 及 Linux 的模型 path。Windows WSL2 成功不表示 Linux 桌機已驗證。以每次 JSON evidence 的時間、model revision、host/runtime 紀錄識別結果。

## 紀錄

目前證據：`evidence/pre-model-validation.json`。

| 層級 | 2026-09-15 結果 |
|---|---|
| 單元／契約 | 26 passed |
| 實際 API container | loopback 連入、401、worker 缺席 503、持久化 fsync 通過；測試容器已移除 |
| 官方 worker runtime CUDA | 0.29.0 / torch 2.13.0+cu130 / CUDA 13.0 / transformers 5.16.1；八元素 tensor 計算通過 |
| 既有模型 register／inspect | Qwen3.5-4B 一致 revision、11 個必要檔案，未改動權重 |
| 真實模型 GPU 推論／完整故障驗收 | **待使用者選定模型，尚未執行** |
| 真實 visual／tool roundtrip | **尚未執行** |
| Linux 實體桌機 | **尚未驗證** |

已提供完整驗收程式，但其存在不等於驗收通過。fetch 的固定 revision 行為有契約測試，尚未實際下載外部模型。
