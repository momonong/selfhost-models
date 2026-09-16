# selfhost-models

共用模型推論與 Docker 部署管理。提供固定 HF revision 的模型管理與 vLLM serving，業務 prompt、工具執行與產品規則留在各產品。

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

路徑依 host 調整。Linux MODEL_ROOT 預設 `/srv/selfhost-models/models`，Windows 為 `D:/hf_models`。先完成 register 或獨立 fetch；serve 不下載權重。

API 在 `http://127.0.0.1:18080`；待 `/health/ready` 回 200，再以 `.state/api-key` 的 Bearer token 呼叫 `/v1/models`、`/v1/chat/completions`。支援一般與 SSE、嚴格輸入限制、有界 admission 與可追蹤的取消／逾時。

## 文件與驗證

- [架構與生命週期](docs/architecture.md)
- [API 支援範圍與限制](docs/api.md)
- [資產管理、Windows／Linux 部署](docs/deployment.md)
- [可重跑驗收與證據](docs/acceptance.md)
- [Backend 契約與 Transformers 交接](docs/backend.md)
- [模型候選與 GPU 盤點](docs/model-selection.md)

已在 Windows WSL2／RTX 5090 Laptop 24 GB 上完成 **Qwen3.5-4B 真實 GPU 驗收**：一般／SSE、合成圖片、工具呼叫往返、超載、逾時／取消及重啟恢復。單元／契約測試 27 項通過。這是服務與合成輸入驗收，Linux 實機及產品品質仍待各自驗證；詳見驗收文件。

```bash
uv run --locked pytest -q
uv run --locked python scripts/acceptance.py --faults
```

第二個指令需要已 ready 的真實服務，會對本專案容器作受控 pause/restart。契約測試不取代 GPU 驗收。
