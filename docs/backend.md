# Backend 契約與 Transformers 交接

下一個任務可引用本文件與 `selfhost_models/backend.py`；不需要 plugin registry 或自動 discovery。

目前 `VLLMBackend` 提供四個 operation：

| Operation | 語義 |
|---|---|
| `identity()` | 健康且可回應的 worker 之 engine epoch；失敗拋例外。epoch 必須隨 engine 整體重啟改變。 |
| `warmup(model, epoch)` | 對指定模型真實生成，檢查完整結果與相同 epoch；不得在此下載模型。 |
| `generate(payload, request_id)` | async context manager，回 HTTP response 與有界可讀內容；含 x-worker-epoch。 |
| `close()` | 關閉傳輸。關閉不等於 GPU abort。 |

Gateway 持有 durable lease，backend 不得默默切模型、fallback 或無限重試。一次 dispatch 至多送一次 generation；未知結果不能自動重送。一般與 SSE response 使用本 repo 明確支援的 OpenAI 子集。

## 完成與取消

Gateway 目前支援 `drain_to_terminal`，沒有可確認的強制 abort RPC。產品取消只表示不再等待結果。實作 Transformers 時必須選擇：

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
