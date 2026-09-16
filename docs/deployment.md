# 模型資產與部署

## Host

- Windows：NVIDIA driver + Docker Desktop WSL2 backend；模型根目錄預設 `D:/hf_models`。
- Linux：NVIDIA driver、Docker Engine、Compose v2、NVIDIA Container Toolkit；預設 `/srv/selfhost-models/models`。
- `MODEL_ROOT` 環境變數或 `uv run --locked modelctl --model-root ...` 可覆寫。host driver 不進 image。
- uv >=0.11.21,<0.13、Python 3.11–3.13（`.python-version` 預設 3.12）；`uv sync --locked`。
- 先 `uv run --locked modelctl doctor` 確認 GPU 型號、用量、現有 compute process、容器與 Docker Linux backend。doctor 是盤點，不會自動停止其他服務，也不是 GPU 推論成功證據。

## uv 套件管理

支援 uv 0.11.21–0.12.x；已安裝者直接執行 `uv --version`、`uv sync --locked`，不需重新安裝。以下只供尚未安裝的主機使用；新安裝建議使用本次驗證的官方 0.12.15，Docker 工具也固定為 0.12.15：

```powershell
# Windows PowerShell；完成後重開終端機，以載入 PATH
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.12.15/install.ps1 | iex"
```

```bash
# Linux
curl -LsSf https://astral.sh/uv/0.12.15/install.sh | sh
```

在 repo 根目錄執行 `uv sync --locked`，uv 會使用或下載 Python 3.12，建立 `.venv` 並依 `uv.lock` 安裝（包含 dev 群組）。不需要手動 activate。已有 Python 時可用 `uv sync --locked --python 3.11` 明確選擇支援版本。完整指令使用 `uv run --locked` 前綴；`--locked` 會拒絕 pyproject 與鎖檔不一致，避免執行時意外重新解析依賴。

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked modelctl --help
# 新增執行期／開發依賴
uv add package-name
uv add --dev package-name
# 有意識地更新指定套件；直接依賴若固定版本，須一併更新版本宣告
uv lock --upgrade-package package-name
```

提交 `pyproject.toml` 與 `uv.lock`；不再維護 `requirements.lock`。遷移時以舊鎖檔版本建立初始 uv lock，避免順便升級依賴。`hf-xet` 的平台條件由 huggingface-hub 的套件 metadata 解析。

API Dockerfile 從固定 digest 的官方 uv image 取得工具，以 `uv sync --locked --no-dev --no-editable` 安裝；容器啟動直接執行 `.venv` 中的 Python，不在啟動時同步或下載。vLLM worker 繼續使用其官方 CUDA／PyTorch 配套。參考 [uv 官方 Docker 指引](https://docs.astral.sh/uv/guides/integration/docker/) 與 [鎖定／同步說明](https://docs.astral.sh/uv/concepts/projects/sync/)。

## 接入既有模型

```powershell
uv run --locked modelctl register Qwen/Qwen3.5-4B --path D:/hf_models/Qwen3.5-4B
uv run --locked modelctl list
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
```

register 不搬移、不改寫、不下載。若每個推論必要檔案都有一致 HF local metadata，離線推得 revision。缺來源資訊時需提供經確認的完整 `--revision <40-hex-commit>`，文件將標為 operator-supplied，不假稱遠端逐位元驗證。

registry 保存 repo ID、固定 revision、absolute path、檔案大小／mtime、小檔 SHA256。權重 shard 以存在、大小與 mtime 檢查；未做完整權重 hash。缺 shard／tokenizer 或檔案變動會拒絕 serve。不要在掛載期間改動模型來源。

## 下載（獨立於 serve）

```bash
uv run --locked modelctl fetch organization/model-name --revision main
uv run --locked modelctl inspect organization/model-name
```

首次 fetch 先解析到完整 commit，下載到 `MODEL_ROOT/managed/<org>--<model>/<sha>`，再原子寫入 registry。之後 fetch/serve 使用已登錄 revision，不重查 main。HF_TOKEN 只從執行環境讀取，不傳入 serving image／Compose。gated repo 權限由使用者既有授權決定。

registry 已有另一組 path/revision 時拒絕覆寫。下載中斷留下未登錄目錄：先檢查完整度，完整者 register；本版不自動刪除／覆寫或偷偷補完未知資產。另選 `--state` 可建立獨立資產 registry，但 Compose project 仍固定 selfhost-models，不能用來同時開第二套服務。

## 啟動

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
uv run --locked modelctl status
```

明確選擇 Transformers（先停止既有部署，不同時載入兩種 backend）：

```bash
uv run --locked modelctl stop
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend transformers --max-inflight 1 --context 2048 --gpu-memory 0.60
uv run --locked modelctl status
```

`--backend` 預設 vllm，寫入 `.state/compose.env` 的 `BACKEND`。Transformers 容量省略時為 1，指定 >1 拒絕；vLLM 省略時維持 2。modelctl 及驗收腳本透過 `compose_command(state)` 根據保存的 BACKEND 選擇 Compose 檔案；Transformers 追加 `compose.transformers.yaml`，取代 worker image/command。手動操作必須帶兩個 `-f`，不能只使用基底設定：

```bash
docker compose --env-file .state/compose.env -f compose.yaml -f compose.transformers.yaml logs --tail 80 worker
```

Transformers 獨立使用 `worker/transformers/pyproject.toml` + `uv.lock`，不是 API 的第二份 requirements 鎖檔。官方 `pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime` 固定 digest `sha256:db80a41f8428644cebcb3d75b0b62df334ab6c0e75785951eb25f48bfbd42407`，提供 Python 3.12.3、PyTorch 2.13.0+cu130、CUDA 13.0 及 torchvision 配套。image 建置用 uv 0.12.15 建立繼承 system-site-packages 的獨立 venv，再 `uv sync --locked --no-dev` 安裝固定 Transformers 5.16.1 與 HTTP 依賴；不重裝基底 torch，也不變更 vLLM 套件組合。建置及 engine 初始化均檢查 runtime 版本。

若修改 worker Python 依賴，使用 `uv lock --project worker/transformers` 更新其 uv.lock。這個專案的 Python 限 3.12，CUDA/PyTorch 由固定 image 提供，host 不需安裝 GPU Python 套件。worker 以 UID 10001、唯讀 filesystem 運行，`/tmp` 保存 HF 暫存；獨立 1 GiB `/runtime-cache` tmpfs 明確允許 exec，供官方 PyTorch/Triton 的 native kernel 編譯與載入。TRITON_CACHE_DIR、TORCHINDUCTOR_CACHE_DIR、CUDA_CACHE_PATH、TMPDIR 指向該區，停止後暫存釋放，模型仍唯讀。啟動不執行 uv sync；模型必須已由 modelctl register/inspect 確認，不改動既有檔案。

架構支援依 [Transformers Qwen3.5 官方文件](https://huggingface.co/docs/transformers/main/model_doc/qwen3_5)，並已在固定 image 離線匯入對應類別核對；runtime 基底依 [官方 PyTorch image](https://hub.docker.com/layers/pytorch/pytorch/2.13.0-cuda13.0-cudnn9-runtime/images/sha256-db80a41f8428644cebcb3d75b0b62df334ab6c0e75785951eb25f48bfbd42407)。支援架構不等於任意 checkpoint 都已實測，實際範圍見驗收文件。

Transformers 明確設定 `stop_grace_period: 30s`，留時間給 PyTorch/CUDA process 正常退出。本機未指定時的容器 StopTimeout 曾實際為 1 秒，uvicorn 已完成 lifespan shutdown，Docker 仍於 1 秒後送 SIGKILL，得到 137（非 OOM）；因此不依賴 host 隱含預設。若停止時仍有長時間工作，30 秒後仍可能強制停止，未決 lease 保留到新 epoch＋warmup 恢復；正常收尾先確認 inflight=0。

一般啟動不連 HF；worker HF_HUB_OFFLINE／TRANSFORMERS_OFFLINE，模型唯讀掛在 `/models/current`。管理工具依 config.model_type 選已知 profile；不自動嘗試不同 engine。

API 預設 `http://127.0.0.1:18080`，先輪詢 `/health/ready`。第一次建立 `.state/api-key`，client 從檔案讀 Bearer token；不將 token 貼入聊天、Git 或日誌。Linux key file mode 600，Windows 使用所在目錄既有 ACL；依需要自行收緊使用者權限。

設定寫入已忽略的 `.state/compose.env`。API 連入口 network 與推論內網，worker 只連推論內網且不暴露 host port。API 的持久狀態 bind mount 在 `.state/runtime`，不能在 worker 還運算時刪除。Linux API 使用執行 modelctl 的 UID/GID，以讀取 mode 600 的 key 與寫入 mode 700 的 state；請以一般使用者操作已配置好的 Docker 權限。Windows 容器使用 UID 10001。

```bash
uv run --locked modelctl stop
uv run --locked modelctl restart
# 精準維運（只操作本專案）
docker compose --env-file .state/compose.env logs --tail 80 worker
docker compose --env-file .state/compose.env restart worker
```

serve 在既有 selfhost-models 容器運行或 port 已用時拒絕接管。restart 明確重啟本專案 worker 與 API；worker-only restart 也由 API 自動偵測新 epoch、暖機並恢復。需要更改部署參數時先 stop，再 serve。stop 不刪除容器、資產或 state。

固定 runtime 版本與實際 image digest、CUDA/PyTorch 組合見驗收紀錄。eager mode、短 context、小併發先建立可預期基準，後續另做 throughput、CUDA graphs 與記憶體調校；不以此次 smoke test 宣稱產品品質或 Linux 實機驗證。

## WSL2 路徑、權限與固定 image 複驗

Ubuntu WSL2 可用 Linux CLI 連到 Docker Desktop，但仍屬 Windows 筆電驗證；Linux 桌機的 Engine、driver 與 NVIDIA Container Toolkit 要另外驗收。2026-09-16 WSL2 複驗使用既有 `/mnt/d/hf_models/Qwen3.5-4B`，沒有複製或下載模型。

模型可放在既有 Windows 磁碟掛載，secret 與可寫 state 則放在 WSL 原生檔案系統，例如 `$HOME/.local/state/selfhost-models/vllm-acceptance-20260916`。一般未啟用 metadata 的 `/mnt/d` 不具完整 Linux chmod 語義，不應由 chmod 成功就宣稱 key 已受 mode 600 保護。每個作業系統使用各自的 uv 環境；本次 WSL 使用 worktree 的 `.venv`，Windows 使用 `UV_PROJECT_ENVIRONMENT=.venv-windows`。

Windows Git 建立的 worktree，其 `.git` 可能含 `D:/...` 絕對路徑。若直接從 WSL 操作該 worktree，先在當次 shell 設定對應 Linux 路徑，不修改其他任務的 Git 設定：

```bash
export GIT_DIR=/mnt/d/projects/selfhost-models/.git/worktrees/linux-vllm-acceptance
export GIT_WORK_TREE=/mnt/d/projects/selfhost-models/.worktrees/linux-vllm-acceptance
# 僅在既有 Windows checkout 使用 CRLF 時對齊 Windows Git 的判斷。
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.autocrlf GIT_CONFIG_VALUE_0=true
cd "$GIT_WORK_TREE"
```

原生 Linux clone 不需要以上轉換。切勿讓 Windows 與 WSL 同時操作同一 worktree 的 index。

`modelctl serve` 預設會 build。若要重用已驗證的**完全相同 image**，先確認本機 `docker image inspect` 的 ID／RepoDigests，再以 host 專用 Compose override 加上 `--no-build --pull never` 啟動。僅有 Dockerfile 固定基底 digest，不代表重新 build 的應用程式 image 相同。

以下為本次主機設定範例；在 repo 根目錄執行，並先確認沒有另一個 `selfhost-models` 部署運行、GPU 時段已協調且 port 18081 可用。`STATE_DIR` 必須是已確認的本次專用目錄，勿覆寫其他部署的設定。首次登錄後應 inspect 並核對 revision 為 `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`：

```bash
STATE_DIR="$HOME/.local/state/selfhost-models/vllm-acceptance-20260916"
uv run --locked modelctl --state "$STATE_DIR" register Qwen/Qwen3.5-4B --path /mnt/d/hf_models/Qwen3.5-4B
uv run --locked modelctl --state "$STATE_DIR" inspect Qwen/Qwen3.5-4B
# 首次建立 key，不覆寫既有 key；內容不可輸出至日誌。
umask 077
mkdir -p "$STATE_DIR/runtime"
chmod 700 "$STATE_DIR" "$STATE_DIR/runtime"
if [ ! -e "$STATE_DIR/api-key" ]; then
  uv run --locked python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE_DIR/api-key"
fi
chmod 600 "$STATE_DIR/api-key"
cat > "$STATE_DIR/compose.env" <<EOF
MODEL_ID=Qwen/Qwen3.5-4B
MODEL_REVISION=851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a
MODEL_PROFILE=qwen3_5
MODEL_PATH=/mnt/d/hf_models/Qwen3.5-4B
API_KEY_PATH=$STATE_DIR/api-key
API_STATE_PATH=$STATE_DIR/runtime
API_UID=$(id -u)
API_GID=$(id -g)
API_PORT=18081
GPU_DEVICE=0
GPU_MEMORY_UTILIZATION=0.60
MAX_MODEL_LEN=2048
MAX_INFLIGHT=2
DEADLINE_SECONDS=30
DRAIN_SECONDS=120
VLLM_USE_V2_MODEL_RUNNER=0
EOF
cat > "$STATE_DIR/images.json" <<'EOF'
{"services":{"api":{"image":"selfhost-models-api@sha256:3ec4784560e022bdcf50ef655164b61985e026e480a0ebda686237a7d8b3b69e"},"worker":{"image":"selfhost-models-vllm@sha256:419b067bd636b368b7bcb714ac5b811c48defdd2f16cc7aaab62171228102173"}}}
EOF
docker compose --env-file "$STATE_DIR/compose.env" -f compose.yaml -f "$STATE_DIR/images.json" up -d --no-build --pull never
```

以上應用程式 image 是本機驗證資產，未宣稱已發布至 registry。其他主機若沒有該 digest，應先安排 image 移轉或記錄重建差異，不移除 digest 來隱性改用新 image。`GPU_DEVICE` 與 `API_PORT` 由 host 設定；Linux 原生模型路徑依既有資產調整，預設根目錄仍為 `/srv/selfhost-models/models`。

依驗收文件跑完後，以同一組 `--env-file`／`-f` 參數執行 `stop`，確認 exit code、未 paused、port 關閉、lease 清空及其他容器未變。保留模型與 state。Compose project 名稱仍固定 `selfhost-models`，不同 state、port 或 worktree 都不表示可以同時啟動兩套服務。

### WSL2 runner 設定

官方 vLLM 0.29.0 的 V2 model runner 在此 WSL2／Blackwell host 因 `UVA is not available` 無法初始化。Compose 明確預設 `VLLM_USE_V2_MODEL_RUNNER=0`，使用同版本內的 V1 runner；不替換 CUDA/PyTorch、不自動改引擎。Linux 若另驗證 V2，可顯式覆寫環境變數後重建 worker；目前 Linux 未實測。

上游對應問題：https://github.com/vllm-project/vllm/issues/50239
