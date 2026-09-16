# selfhost-models

共用模型推論與 Docker 部署管理。提供固定 HF revision 的模型管理、vLLM 與 Transformers serving，業務 prompt、工具執行與產品規則留在各產品。

## 開始使用

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

- [架構與生命週期](docs/architecture.md)
- [API 支援範圍與限制](docs/api.md)
- [資產管理、Windows／Linux 部署](docs/deployment.md)
- [可重跑驗收與證據](docs/acceptance.md)
- [Backend 契約與 Transformers 執行路徑](docs/backend.md)
- [模型候選與 GPU 盤點](docs/model-selection.md)

已在 Windows WSL2／RTX 5090 Laptop 24 GB 上完成 **Qwen3.5-4B 的 Transformers 真實 GPU 文字推論與生命週期驗收**，同一分支的 vLLM 一般／SSE、圖片、工具、超載、逾時／取消及重啟恢復回歸也通過。CPU 單元／契約共 57 項通過。兩種 backend 依序載入，驗收後 API／worker 均正常停止，模型、state 與證據保留；Linux 實體桌機與 Linux Transformers 仍未驗證。能力差異、可重跑命令、image／commit 與限制見驗收文件。

同日另完成 **Ubuntu 24.04 WSL2 CLI 複驗**：重用相同 image digests，採 port 18081、Linux UID 1000 與原生 WSL state。修正驗收腳本 URL/state 設定及 CRLF/LF 來源比對，Windows／WSL 回歸各 34 項通過；真實 GPU 功能與補驗 runtime 證據獨立保存在 `evidence/2026-09-16-wsl2/`。這仍是 Windows 筆電／Docker Desktop，Linux 實體桌機尚未驗證。

```bash
uv run --locked pytest -q
uv run --locked python scripts/acceptance.py --faults
```

第二個指令需要已 ready 的真實服務，會對本專案容器作受控 pause/restart。契約測試不取代 GPU 驗收。
