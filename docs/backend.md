# Backend 契約與 Transformers 執行路徑

`selfhost_models/backend.py` 以明確的 `BACKEND=vllm|transformers` 選擇實作，不做 plugin discovery 或 fallback。未設定值時保留既有 vLLM 預設；modelctl 每次 serve 都把實際選擇寫入部署設定。

`VLLMBackend` 與 `TransformersBackend` 共用四個 operation：

| Operation | 語義 |
|---|---|
| `identity()` | 健康且可回應的 worker 之 engine epoch；失敗拋例外。epoch 必須隨 engine 整體重啟改變。 |
| `warmup(model, epoch)` | 對指定模型真實生成，涵蓋多 token decode、部署容量與已啟用能力。Transformers 單請求、文字 profile 使用內部固定 4-token 暖機；vLLM 保留併發與視覺暖機。檢查結果與相同 epoch，總上限 180 秒，不得下載模型。 |
| `generate(payload, request_id)` | async context manager，回 HTTP response 與有界可讀內容；含 x-worker-epoch。 |
| `close()` | 關閉傳輸。關閉不等於 GPU abort。 |

Gateway 持有 durable lease，backend 不得默默切模型、fallback 或無限重試。一次 dispatch 至多送一次 generation；未知結果不能自動重送。一般與 SSE response 使用本 repo 明確支援的 OpenAI 子集。

## 完成與取消

兩種 backend 都使用 `drain_to_terminal`，沒有可確認的強制 abort RPC。產品取消只表示不再等待結果。未來若增加不同取消路徑，必須維持下列其中一種語義：

1. 同樣持續執行／drain，直到完整 terminal；或
2. 新增 request-scoped cancel + terminal acknowledgment（明確表示 scheduler 已移除且不再持有執行資源），並新增契約與真 GPU 驗收。

收到 client disconnect、HTTP 連線關閉、RPC 接受取消、單纯 asyncio task cancellation 都不是 terminal acknowledgment。不能在 worker 還跑時 release lease。API crash 後的未知工作同樣必須保守恢復。

## 實作邊界

- Transformers image 自己固定 CUDA/PyTorch/Transformers 組合；不得替換 vLLM image 裡的 PyTorch。
- 以 `/models/current` 唯讀資產啟動、禁止 online fetch。
- 每個實際 engine lifecycle 一個 epoch；health、warmup、generation 都回相同 identity。
- 工具執行、agent loop、業務 prompts 不進 worker；只產生結構化模型輸出。
- worker 能力不同時明確拒絕不支援參數；不得宣稱所有 OpenAI endpoint、模態、工具格式都通用。
- 重用契約測試，另外提供合成輸入的真實 GPU 證據。host、Windows Docker、Linux 實體部署結果分別報告。

## Transformers worker

`worker/transformers_app.py` 為單 ASGI process；`worker/transformers_engine.py` 是固定 Qwen3.5 adapter。只接受 `qwen3_5` / `Qwen3_5ForConditionalGeneration` 非量化 checkpoint，以 BF16 載入完整權重到 CUDA 0，使用 SDPA；不使用 `device_map=auto`、CPU offload、自訂 HF code 或量化 fallback。載入呼叫均 `local_files_only=True`、`trust_remote_code=False`，模型唯讀且 worker 無對外網路。

目前只宣告文字（含 user text parts）、一般 JSON、SSE、usage、temperature/top_p/seed/max_tokens。thinking 固定關閉；不支援圖片、工具或工具 history、stop 字串、非零 presence/frequency penalties。`temperature=0` 時要求 `top_p=1`，避免忽略取樣設定。context 是套用模板後的輸入 token + 最大輸出 token，部署範圍 128–2048，輸出最多 1024。這不是任意 HF 模型載入器。

worker 同時至多一個執行工作，等待佇列 0。獨立持有的 asyncio task 負責 CPU tokenization 與 GPU thread，HTTP handler 只能等待它，不能取消它。超載直接拒絕，不將請求排進 executor 等待佇列。只有生成返回、結果轉回 CPU 且 `torch.cuda.synchronize()` 成功後，才產生 terminal response 並釋放 worker 執行名額。`TextStreamer.end()` 不被當成底層完成確認。

SSE 最多緩衝 16 段文字；緩衝溢位或 socket 寫出逾時時停止交付、保留運算直到完成，沒有成功 terminal 就不送 `[DONE]`。一般回應不受 SSE buffer 溢位影響。CUDA/engine 例外使 worker health 失敗並回泛化錯誤，gateway 保留未知 lease，須重啟 worker 產生新 epoch 並重新暖機；不在同 epoch 自動重試。process 的 epoch 與其 thread/CUDA context 一起消亡；不支援外接 engine、多 frontend 或多 process。

worker `/health` 表示權重已載入且 engine 無故障；對外 API `/health/ready` 還要求 durable warmup 成功。API 重啟而 worker 未重啟時，journal 中未決工作仍隔離，即使 worker 後來完成，也不能憑健康 probe 釋放未知 lease。
