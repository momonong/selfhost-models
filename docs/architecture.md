# 架構與執行語義

## 責任

產品負責領域 prompt、產品規則、工具執行、人工校正與成效驗證。此 repo 負責固定模型資產、共用推論 API、Docker 部署與服務生命週期。

`client → loopback API:18080 → internal worker:8000 → vLLM → GPU`

統一 CPU API image；worker 基於固定官方 vLLM image。未來 Transformers 是另一個 image／backend，不把兩套 runtime 混裝。沒有 Kubernetes、Docker socket、自動換模或失敗 fallback。Windows driver 在 Windows host，透過 Docker Desktop WSL2 執行 Linux container；Linux 使用 Engine + NVIDIA Container Toolkit。

## 容量、deadline 與完成確認

- 一個 uvicorn process、一個 vLLM frontend／engine、一個指定模型。
- API 最多 32 個已授權 HTTP handler；body 最多 2 MiB、讀取最多 10 秒。推論預設最多 2 個 lease，等待佇列長度 **0**；超出立即 429 + Retry-After。
- vLLM 的 max-num-seqs 與 admission 上限一致。vLLM 自行管理 batching、KV cache；API 不實作 GPU scheduler。
- lease 釋放表示該請求已終止、可接收下一個請求，不表示模型權重或整個 KV 記憶體池已還給作業系統。服務常駐時 vLLM 仍保留配置的 VRAM；需要讓其他工作使用這些 VRAM 時，由管理命令停止 worker。
- deadline 預設 30 秒，涵蓋 body 接收、worker 執行、下游寫出。有限 body/輸出/token 上限避免無界累积。
- 一般回應完整且含 finish_reason，或 SSE 收到 `[DONE]` 才算協定層的終止確認。400/404/422 的 worker 驗證拒絕也可釋放 lease。5xx、傳輸中斷、格式錯誤都視為不確定。
- 客戶端斷線／deadline：停止交付，producer 繼續接收直到終止。**本版沒有立即 GPU abort 能力**；最多追蹤到 drain deadline（預設從 dispatch 起 120 秒）。drain 到期會關閉連線並標為不確定，不宣稱 GPU 已停。
- SSE 16 個 chunk buffer；客戶端過慢就 detach，繼續 drain worker。已送 HTTP 200 後的失敗用 SSE `error` 物件，結束且不補 `[DONE]`。
- 發送前 fsync lease journal；API crash/restart 後同 worker epoch 的未決 lease 使服務不 ready。只有新 worker epoch 加上成功 warmup 可清除舊 epoch 的未決工作。不要刪 state volume 來繞過此保護。
- worker identity 是純 ASGI middleware，每個 frontend process 產生隨機 epoch。此保證依賴單 frontend 與其 engine 一起啟停，禁止多 frontend 或外接可獨立生存的 engine。
- readiness 每 2 秒 probe；新 engine 必須通過 4 token 的 prefill＋decode、設定容量的併發暖機，以及視覺 profile 的合成單圖暖機。初始化暖機總上限 180 秒，獨立於 client deadline；整段 warmup 有持久化 lease。暫時失聯後同 epoch 恢復時沿用已完成的 warmup。不健康／未決時回 503。這不保證所有未見過的輸入形狀都沒有 JIT latency。

## 安全與紀錄

API 綁定 127.0.0.1；worker 無 published port，僅內部 Docker network。API 使用 Bearer secret（管理工具建立本機 key file，由 Compose secret 掛載），health endpoints 不要求 key。沒有雲端 fallback、執行工具、任意模型路徑或外部 URL 抓取。

紀錄 request ID、服務／模型版本、耗時、完成確認、detach 與錯誤代碼；不紀錄原始 prompt、圖片、工具參數或 worker 原始錯誤。vLLM 關閉 request/access logging 與 usage telemetry。worker 的引擎啟動／故障診斷仍是 upstream log，不作為可含敏感資料的持久稽核管道。

API 只有 state volume 可寫，非 root；模型目錄唯讀、HF offline。模型既有檔案由 owner 管理，register 的尺寸／mtime／小檔 SHA256 是變更偵測，不是完整權重密碼學驗證。不要在服務期間修改來源目錄。
