# selfhost-models 開發規則

- 修改前核對 HEAD、分支、worktree 與 dirty state；工作分支採 `<type>/<short-description>`。
- 共用模型 API 不含業務 prompt、工具實際執行、人工校正或產品驗證。
- API、vLLM、未來 Transformers 使用獨立 image；GPU driver 留在 host。
- 固定官方推論 runtime 配套，不單獨替換 PyTorch；模型使用固定 revision、唯讀掛載，啟動與請求不得下載。
- API 不掛載 Docker socket。單一 API process；擴充副本前必須重新設計全域 admission 與持久化 lease。
- 取消／逾時不代表 GPU 已停止；維持 backend 文件中的完成確認與重啟恢復語義。
- Python 3.11–3.13；`python -m pip install -r requirements.lock`、`python -m pip install --no-deps -e .`。
- 契約測試：`python -m pytest -q`。GPU 驗收入口見 `docs/acceptance.md`；不得將 mock 測試描述成 GPU 驗證。
- GPU／Docker 故障注入只針對本專案 Compose 容器，執行前盤點共用資源。
- 文件入口：README；架構與 backend 契約分別在 `docs/architecture.md`、`docs/backend.md`。
