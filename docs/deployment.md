# 模型資產與部署

## 影片 opt-in 部署

先協調共用服務的使用時段、確認既有 lease 歸零。停止現有部署後才執行：

```powershell
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend vllm --video --context 8192 --max-inflight 2 --gpu-memory 0.60
```

只允許固定 revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，context 必須 8192、最多 2 個名額；不自動改設定或 fallback。`modelctl` 在私有 compose.env 寫 `VIDEO_ENABLED=1`／`API_MEMORY_LIMIT=2g`；未啟用仍關閉影片，API memory 預設 512m。兩種配置的 API 都有 2 CPU、64 pids、128 MiB `/tmp` tmpfs。影像 budget 與影片總 budget 在 gateway 到 worker 邊界分開，沒有更換 vLLM runtime。

只有 `/health/ready` 成功且 `/v1/models` 宣告 videos 才能送影片；暖機包含最大影片形狀。新建容器的 JIT 可能較久，不因等待而刪除 lease state。初始化失敗／未知工作仍須保留日誌，按既有新 worker epoch 恢復流程處理。完整 API 限制與重跑證據見 [API](api.md)、[驗收](acceptance.md)。影片版已納入 0.2.0 發布，固定 images 見本文 Docker Hub 章節。

## Linux 桌機交接入口

影片功能來源為 `841132e`，0.2.0 發布來源為 `29bec681f6577f88067edf8cca9952e66b58232c`；桌機 checkout 須包含後者及本文件。三個公開 repositories 使用 `0.2.0` 與 `latest`，舊 `sha-4dc0a81` 已移除。先依本文 Docker Hub 章節拉取固定 images，再用 `--no-build`，或選擇從原始碼建置並另行記錄產物。

### 1. 取得來源與盤點主機

在桌機既有 checkout 操作前，先確認沒有其他任務使用該目錄；沒有 checkout 才 clone，不建立額外 worktree。核對 `git status --short`、`git worktree list`、`git rev-parse HEAD` 與 `git log -5 --oneline`，不要強制重設既有工作。

```bash
git fetch origin
git merge-base --is-ancestor 29bec681f6577f88067edf8cca9952e66b58232c HEAD
# 上一行非零表示目前 checkout 尚未包含影片交付，先解決版本問題。
uname -m
cat /etc/os-release
nvidia-smi
docker version
docker compose version
nvidia-ctk --version
uv --version
uv sync --locked
uv run --locked modelctl doctor
```

這組 runtime 的桌機目標為 Linux x86_64／NVIDIA GPU。不要只比較 VRAM 容量；GPU 架構、host driver 與各 image 的 CUDA runtime 也要相容。driver 留在 host，PyTorch／CUDA 依 Dockerfile 固定配套；不另裝 host PyTorch 來修容器。缺 driver、Docker 或 NVIDIA Container Toolkit 時，由桌機任務依實際發行版處理；涉及 Docker 重啟前先協調其他服務。不要因為筆電用 WSL2 就在 Linux 桌機安裝 WSL。

記錄實際 HEAD、dirty state、GPU 型號／總量／已用 VRAM、driver、Docker／Compose 與 Toolkit 版本。筆電影片的 context 8192、並行 2、GPU fraction 0.60 是驗證起點，不保證桌機相同設定可用。沿用預設 V1 runner；切到 V2 屬另一項配置變更，需獨立驗證。

### 2. 模型及主機狀態

不要複製筆電 `.state`、API key、leases.json、compose.env、`.venv` 或實驗 override。桌機使用自己的 state 與 key；以下預設是桌機 repo 下的 `.state`，由 modelctl 產生 host 路徑與 UID/GID。以同一個有 Docker 權限的普通使用者執行，避免混用 sudo 造成檔案擁有者不同。

先找到已存在的完整模型目錄，再登錄（範例路徑需依實際資產修改）：

```bash
uv run --locked modelctl register Qwen/Qwen3.5-4B --path /srv/selfhost-models/models/Qwen3.5-4B
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
```

必須核對 revision 為 `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`。保留來源 metadata；缺 metadata 時，只有能以移轉清單等證據確認來源，才提供明確 `--revision`，不能用旗標掩蓋未知來源。移轉權重時核對檔案清單及 SHA256；不要搬動筆電正在掛載的原模型。

若沒有資產，另行選擇移轉或下載。獲授權下載後使用 `uv run --locked modelctl fetch Qwen/Qwen3.5-4B --revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，確認預設 `/srv/selfhost-models/models` 可寫；需要其他位置時，所有相關 modelctl 命令一致使用 `--model-root <實際目錄>`（放在子命令前）。HF 真實下載仍待桌機實測，啟動及請求不下載模型。

### 3. 驗收後常駐使用

先完成 [Linux 桌機驗收](acceptance.md#linux-桌機實機驗收待執行)。`modelctl serve --no-build` 使用已拉取的固定 images；省略選項才從 checkout 建置 API／選定 worker。保存桌機實際 image ID 與 runtime 證據；固定原始碼不代表重建 digest 必然與筆電相同。磁碟需容納 runtime、build cache 與權重。

驗收結束且兩 backend 均已停止後，若選定影片服務作日常部署：

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend vllm --video --context 8192 --max-inflight 2 --gpu-memory 0.60 --no-build
uv run --locked python scripts/chat.py --wait 600 --stream 'Reply with READY.'
```

依 [Client 接入指南](client-integration.md) 安全讀取桌機 `.state/api-key`，檢查 `/health/ready` 及 `/v1/models` 的 model/revision/backend/videos。其他專案的設定必須改用桌機本地路徑，不沿用 Windows key 路徑。`127.0.0.1:18080` 只代表桌機自己；筆電不會因此連上桌機。本次不開放 LAN／公網，也不假設其他容器可連線。

目前沒有開機自啟；主機重新啟動、Docker ready 後用 `uv run --locked modelctl restart`，再驗證 readiness。初始化失敗先保存 logs，不刪 lease journal。交接回報應分開列出：各 backend／影片驗收結果、最終運行 backend、端口、image IDs、剩餘問題；桌球品質仍需產品端人工評估。

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

公開 image 的版本與拉取方式見本文末尾的 [Docker Hub images](#docker-hub-images)。

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

## 驗收腳本與自訂部署

`capture_runtime.py`、`stream_lifecycle_check.py` 和 `acceptance.py --faults` 會先核對 URL 為設定的 HTTP loopback port、state key 與容器 secret 相同、Compose 設定 hash／backend 與所選 API／worker 一致。錯配在擷取或故障注入前拒絕。digest override 只有在 image ID 與當前設定的 image 完全相同、其餘部署設定未變時才接受；不同內容的歷史 image 必須搭配對應 checkout 驗收。

例如已從此 checkout 啟動 `--state .state/integration-vllm`、port 18083 的部署，腳本使用同一組參數（輸出改用新的目錄）：

```bash
uv run --locked python scripts/capture_runtime.py --url http://127.0.0.1:18083 --state .state/integration-vllm --output evidence/rerun/runtime.json
uv run --locked python scripts/stream_lifecycle_check.py --url http://127.0.0.1:18083 --state .state/integration-vllm --output evidence/rerun/stream-lifecycle.json
uv run --locked modelctl --state .state/integration-vllm stop
```

本次整合的 `.state/integration-transformers`（18082）與 `.state/integration-vllm`（18083）均保留且服務已停止；原 `.state` 未覆寫。需重新啟動時明確選擇 backend／state／port，不可同時啟動兩個 backend。一般 `modelctl serve` 仍會 build；重用 image 時使用經核對的 Compose 設定和 `--no-build --pull never`，並以 runtime source checks 確認內容。

## WSL2 路徑、權限與固定 image 複驗

下列 worktree、state 與 image digest 範例記錄 `e3fb6ed` 歷史驗收；該 worktree 在整合後已清理，原生 WSL state 與證據保留。當前版本一般操作請使用主目錄及上方 modelctl／backend 指令，不直接用舊 image 代表新版本。

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

## Docker Hub images

### 0.2.0 版本標籤規則

每個公開 repository 使用 `0.2.0` 與 `latest`，兩者指向同一 digest。版本標籤發布後不覆寫；latest 僅在整組固定版本驗證後更新。不再新增 SHA tag，來源 commit 寫入 `org.opencontainers.image.revision` label 與發布紀錄。Docker tag 本身可以變動，正式部署仍固定 digest；見 [Docker 官方說明](https://docs.docker.com/build/building/best-practices/#pin-base-image-versions)。

API、vLLM 與 Transformers 使用同一組專案交付版本，發布紀錄分別保存各 image digest、來源 commit、runtime 版本與驗證範圍。vLLM 的 `0.29.0` 是上游 runtime 版本，不是 selfhost-models 版本。API、worker 套件與 API 回報版本已同步為 `0.2.0`，上游依賴未升級。一般本機 build 未傳 SOURCE_REVISION 時 label 為 unknown，不冒用已發布來源。

先發布並驗證整組固定版本，再更新各 repository 的 latest。跨 repository 的 tag 更新不是原子操作，因此部署不要依賴三個 latest 永遠在同一瞬間一致。latest 是移動別名，不是 Docker 自動判斷的最高版本，也不會自動更新已運行的容器。

### 已發布的 0.2.0

發布來源為 `29bec681f6577f88067edf8cca9952e66b58232c`。三份 images 都重新建置並核對來源、版本及 runtime 匯入；117 項 CPU 測試通過。推論程式沿用影片驗收版本，新增 CLI no-build 與版本資訊；沒有對重新包裝的 images 重跑完整 GPU 驗收。筆電既有服務未重啟。僅提供 `linux/amd64`；Windows 使用 Docker Desktop WSL2。舊 `sha-4dc0a81` 標籤已移除，舊 evidence 保留供追溯，不再作部署入口。

| Image | 交付 digest |
| --- | --- |
| [momonong/selfhost-models-api](https://hub.docker.com/r/momonong/selfhost-models-api) | `sha256:83ff47579c8b95a45c4448cce2ceba876d755ba9df95066e5237c535e065c5f4` |
| [momonong/selfhost-models-vllm](https://hub.docker.com/r/momonong/selfhost-models-vllm) | `sha256:8d3662676caf1539203644e67fc90188cfc6898c623daac871281f24e5fb59e0` |
| [momonong/selfhost-models-transformers](https://hub.docker.com/r/momonong/selfhost-models-transformers) | `sha256:ae2fee7b2386ec9640eb56a7d95aea8e540331c437616fd5746d63de5ab93e0f` |

依固定 digest 拉取後，給本機 Compose 預設版本名稱；這只變更本機標籤，不修改 registry。若後續自行 build，不能再假設該本機標籤仍為發布產物，需重新拉取／核對：

```bash
docker pull momonong/selfhost-models-api@sha256:83ff47579c8b95a45c4448cce2ceba876d755ba9df95066e5237c535e065c5f4
docker tag momonong/selfhost-models-api@sha256:83ff47579c8b95a45c4448cce2ceba876d755ba9df95066e5237c535e065c5f4 momonong/selfhost-models-api:0.2.0
docker pull momonong/selfhost-models-vllm@sha256:8d3662676caf1539203644e67fc90188cfc6898c623daac871281f24e5fb59e0
docker tag momonong/selfhost-models-vllm@sha256:8d3662676caf1539203644e67fc90188cfc6898c623daac871281f24e5fb59e0 momonong/selfhost-models-vllm:0.2.0
# 只在使用 Transformers 時需要
docker pull momonong/selfhost-models-transformers@sha256:ae2fee7b2386ec9640eb56a7d95aea8e540331c437616fd5746d63de5ab93e0f
docker tag momonong/selfhost-models-transformers@sha256:ae2fee7b2386ec9640eb56a7d95aea8e540331c437616fd5746d63de5ab93e0f momonong/selfhost-models-transformers:0.2.0
```

images 不包含模型權重、host driver、API key 或使用者 state。模型仍須由 host 登錄固定 revision、唯讀掛載。公開發布與可拉取不代表 Linux 實體桌機已驗收。

### 使用下載的 image 建立主機部署

完成桌機主機準備、模型 register／inspect 並拉取 images 後，在本專案沒有運行容器的時候執行：

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend vllm --video --context 8192 --max-inflight 2 --gpu-memory 0.60 --no-build
uv run --locked python scripts/chat.py --wait 600 "Reply with READY."
```

`--no-build` 仍檢查模型與 revision，建立本機 key/state/compose.env，使用 `up --no-build --pull never`；缺 image 就失敗，不 fallback 到 build。省略此選項則沿用原始碼建置。Transformers 先停止 vLLM，再使用 `--backend transformers --context 2048 --max-inflight 1 --no-build`，不加 video。CLI 契約已測試，實際桌機建立與 GPU 驗收仍待執行。

來源與容器驗證見 [images.json](../evidence/2026-09-16-release-0.2.0/images.json)，發布證據見 [docker-hub.json](../evidence/2026-09-16-release-0.2.0/docker-hub.json)。本次運行中的筆電容器保留原 image，發布沒有重啟服務；Linux 桌機與桌球品質仍需分別驗收。
