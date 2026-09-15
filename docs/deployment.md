# 模型資產與部署

## Host

- Windows：NVIDIA driver + Docker Desktop WSL2 backend；模型根目錄預設 `D:/hf_models`。
- Linux：NVIDIA driver、Docker Engine、Compose v2、NVIDIA Container Toolkit；預設 `/srv/selfhost-models/models`。
- `MODEL_ROOT` 環境變數或 `modelctl --model-root ...` 可覆寫。host driver 不進 image。
- Python 3.11–3.13；`python -m pip install -r requirements.lock`，再 `python -m pip install --no-deps -e .`。
- 先 `modelctl doctor` 確認 GPU 型號、用量、現有 compute process、容器與 Docker Linux backend。doctor 是盤點，不會自動停止其他服務，也不是 GPU 推論成功證據。

## 接入既有模型

```powershell
modelctl register Qwen/Qwen3.5-4B --path D:/hf_models/Qwen3.5-4B
modelctl list
modelctl inspect Qwen/Qwen3.5-4B
```

register 不搬移、不改寫、不下載。若每個推論必要檔案都有一致 HF local metadata，離線推得 revision。缺來源資訊時需提供經確認的完整 `--revision <40-hex-commit>`，文件將標為 operator-supplied，不假稱遠端逐位元驗證。

registry 保存 repo ID、固定 revision、absolute path、檔案大小／mtime、小檔 SHA256。權重 shard 以存在、大小與 mtime 檢查；未做完整權重 hash。缺 shard／tokenizer 或檔案變動會拒絕 serve。不要在掛載期間改動模型來源。

## 下載（獨立於 serve）

```bash
modelctl fetch organization/model-name --revision main
modelctl inspect organization/model-name
```

首次 fetch 先解析到完整 commit，下載到 `MODEL_ROOT/managed/<org>--<model>/<sha>`，再原子寫入 registry。之後 fetch/serve 使用已登錄 revision，不重查 main。HF_TOKEN 只從執行環境讀取，不傳入 serving image／Compose。gated repo 權限由使用者既有授權決定。

registry 已有另一組 path/revision 時拒絕覆寫。下載中斷留下未登錄目錄：先檢查完整度，完整者 register；本版不自動刪除／覆寫或偷偷補完未知資產。另選 `--state` 可建立獨立資產 registry，但 Compose project 仍固定 selfhost-models，不能用來同時開第二套服務。

## 啟動

```bash
modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
modelctl status
```

一般啟動不連 HF；worker HF_HUB_OFFLINE／TRANSFORMERS_OFFLINE，模型唯讀掛在 `/models/current`。管理工具依 config.model_type 選已知 profile；不自動嘗試不同 engine。

API 預設 `http://127.0.0.1:18080`，先輪詢 `/health/ready`。第一次建立 `.state/api-key`，client 從檔案讀 Bearer token；不將 token 貼入聊天、Git 或日誌。Linux key file mode 600，Windows 使用所在目錄既有 ACL；依需要自行收緊使用者權限。

設定寫入已忽略的 `.state/compose.env`。API 連入口 network 與推論內網，worker 只連推論內網且不暴露 host port。API 的持久狀態 bind mount 在 `.state/runtime`，不能在 worker 還運算時刪除。Linux API 使用執行 modelctl 的 UID/GID，以讀取 mode 600 的 key 與寫入 mode 700 的 state；請以一般使用者操作已配置好的 Docker 權限。Windows 容器使用 UID 10001。

```bash
modelctl stop
modelctl restart
# 精準維運（只操作本專案）
docker compose --env-file .state/compose.env logs --tail 80 worker
docker compose --env-file .state/compose.env restart worker
```

serve 在既有 selfhost-models 容器運行或 port 已用時拒絕接管。restart 明確重啟本專案 worker 與 API；worker-only restart 也由 API 自動偵測新 epoch、暖機並恢復。需要更改部署參數時先 stop，再 serve。stop 不刪除容器、資產或 state。

固定 runtime 版本與實際 image digest、CUDA/PyTorch 組合見驗收紀錄。eager mode、短 context、小併發先建立可預期基準，後續另做 throughput、CUDA graphs 與記憶體調校；不以此次 smoke test 宣稱產品品質或 Linux 實機驗證。

### WSL2 runner 設定

官方 vLLM 0.29.0 的 V2 model runner 在此 WSL2／Blackwell host 因 `UVA is not available` 無法初始化。Compose 明確預設 `VLLM_USE_V2_MODEL_RUNNER=0`，使用同版本內的 V1 runner；不替換 CUDA/PyTorch、不自動改引擎。Linux 若另驗證 V2，可顯式覆寫環境變數後重建 worker；目前 Linux 未實測。

上游對應問題：https://github.com/vllm-project/vllm/issues/50239
