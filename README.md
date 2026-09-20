# selfhost-models

共用模型推論與 Docker 部署管理。提供固定 HF revision 的模型管理、vLLM 與 Transformers serving，業務 prompt、工具執行與產品規則留在各產品。

## 開始使用

Linux 桌機請從 [桌機交接入口](docs/deployment.md#linux-桌機交接入口) 開始，接著執行 [桌機驗收](docs/acceptance.md#linux-桌機實機驗收待執行)。影片版 0.2.0 提供固定 images 與 `modelctl serve --no-build`；亦可從原始碼建置。

筆電日常啟動、直接對話與 backend 切換，請看 [本機使用指南](docs/local-use.md)；其他專案接入請直接看 [Client 接入指南](docs/client-integration.md)。Linux 桌機尚未驗收，不影響已驗證的筆電使用範圍。

uv 0.11.21–0.12.x、Docker Compose、可用的 NVIDIA GPU container 環境。Python 預設 3.12（支援 3.11–3.13），由 uv 管理：

首次安裝 uv 與依賴更新方式見 [套件管理](docs/deployment.md#uv-套件管理)。

```bash
uv sync --locked
uv run --locked modelctl doctor
uv run --locked modelctl register Qwen/Qwen3.5-4B --path D:/hf_models/Qwen3.5-4B
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
uv run --locked modelctl serve Qwen/Qwen3.5-4B
```

Transformers 文字推論使用 `uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend transformers --max-inflight 1`。
切換前先停止前一個部署；兩種 backend 使用獨立 image，不同時啟動、不自動 fallback。Transformers 固定 PyTorch 2.13.0／CUDA 13.0／Transformers 5.16.1，支援範圍及驗收結果見下列文件。

路徑依 host 調整。Linux MODEL_ROOT 預設 `/srv/selfhost-models/models`，Windows 為 `D:/hf_models`。先完成 register 或獨立 fetch；serve 不下載權重。

API 在 `http://127.0.0.1:18080`；待 `/health/ready` 回 200，再以 `.state/api-key` 的 Bearer token 呼叫 `/v1/models`、`/v1/chat/completions`。支援一般與 SSE、嚴格輸入限制、有界 admission 與可追蹤的取消／逾時。

## 文件與驗證

單機持久排程開發版：見 [模型管理與 durable jobs](docs/scheduler.md)。此為明確 opt-in
模式，CPU／子程序驗證與 GPU 切換驗收分開；不會自動遷移或停止現役 static 服務。

影片以 `--video --context 8192` 明確啟用，只支援固定 Qwen3.5-4B／vLLM。接受 1–60 秒 inline H.264 MP4（≤16 MiB、CFR、≤720p60），有界解碼後約 2fps／最多 120 幀、256²，音軌不處理；Transformers 不支援影片。這是通用影片輸入能力，桌球精彩程度與快速攻防品質尚未驗收。使用方式見 [API](docs/api.md)、[可行性與取捨](docs/video-feasibility.md)、[可重跑驗收](docs/acceptance.md)。

- [架構與生命週期](docs/architecture.md)
- [其他專案 Client 接入指南](docs/client-integration.md)
- [API 支援範圍與限制](docs/api.md)
- [資產管理、Windows／Linux 部署](docs/deployment.md)
- [可重跑驗收與證據](docs/acceptance.md)
- [Backend 契約與 Transformers 執行路徑](docs/backend.md)
- [模型候選與 GPU 盤點](docs/model-selection.md)

已整合 Transformers backend 與 Ubuntu WSL2 驗收成果。Windows／WSL CPU 契約各 **84 項通過**；Windows Docker Desktop WSL2／RTX 5090 Laptop 上，兩 backend 依序完成一般／SSE、實際文字輸出後的 deadline／disconnect、runtime／API／worker 來源比對與正常停機。Transformers 為文字、單請求、queue 0；vLLM 保留圖片與工具能力。來源分支的完整超載／重啟回歸與首次失敗紀錄也保留。

上述整合驗收使用獨立 state 與 port 18082／18083，結束後正常停機並清理 WSL worktree。後續筆電交付新增 UTF-8 對話 client，Windows CPU 測試 **87 項通過**，兩 backend 再次依序通過一般／SSE 與 smoke；交付時 vLLM／API 保持在 `127.0.0.1:18080` ready，使用 `.state`。目前即時狀態請以 `modelctl status` 與 `/health/ready` 為準。

API 與 Transformers worker 的來源有 CRLF／LF 差異，只能稱換行正規化後一致。Linux 實體桌機、Linux Transformers 與 HF 實際下載仍未驗證；詳細結果、桌機驗收步驟及限制見 [驗收文件](docs/acceptance.md)。公開 images 與固定版本使用方式見 [部署文件](docs/deployment.md#docker-hub-images)。

```bash
uv run --locked pytest -q
uv run --locked python scripts/acceptance.py --faults
```

第二個指令需要已 ready 的真實服務，會對本專案容器作受控 pause/restart。契約測試不取代 GPU 驗收。
