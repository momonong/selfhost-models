# 單機模型管理與排程 v1

此模式是明確 opt-in 的 durable async jobs 與模型生命週期控制。既有 static
Compose／Chat／SSE 保留；啟用 managed 模式不代表已通過 GPU 切換或產品品質驗收。
首批固定 Qwen3.5-4B／vLLM 與 Whisper-small／Transformers。資產、runtime 與資源
設定共同決定 deployment，不按模型家族或相同權重合併排程。

## 執行位置與權限

Managed API 與 host controller 在**同機 Linux 或同機 WSL、同一 OS** 執行，SQLite
放 Linux 本機 ext4/xfs/btrfs。不要放 `/mnt/c`、`/mnt/d`、9p/DrvFs、網路磁碟、NAS
或同步資料夾；也不要由 Windows 程序打開這個 SQLite。Windows 是 HTTP client，
`scheduler catalog/submit/get/cancel/result/upload` 只讀指定的 key file、完全不開 DB。
模型唯讀來源可由管理端解析本機路徑；public jobs 沒有 host path／URL／command。

```text
Windows/WSL client --public Bearer--> Linux loopback CPU API
                                      │ shared local SQLite
                               Linux host controller --Docker CLI
                                      │ independent worker secret
                              loopback CPU relay (no GPU)
                                      │ internal Docker network
                             fixed Qwen or Whisper worker
```

API 不掛 Docker socket。Controller 使用固定 argv、`shell=False`、固定 runtime 模板；
沒有 public Docker/shell 操作 endpoint。GPU worker 只接 internal network，不發布
port；CPU relay 才接 ingress + internal network，僅在 host loopback 發布 port。
relay 與 worker 都驗證獨立 internal secret 與 operation allowlist；public Bearer、
裸請求或 client 自訂 internal header不能繞過 public admission。擁有 Docker／主機
檔案管理權限的人仍是管理者；這不是 RBAC 或多租戶安全隔離。

同 Docker daemon 的 GPU 0 共用 `selfhost-gpu-0-owner` named-volume ownership gate。
Docker create 對同名 volume 原子且 idempotent；**create 成功並不代表取得 ownership**，
必須核對保留下來的 owner/mode labels。static CLI 與 managed controller 都經此 gate，
不同 Windows／WSL state 不會各自取得同 GPU。既有未納管 GPU 容器另由 preflight
拒絕。Gate 不因 timeout／heartbeat 到期釋放，不要手動刪它來搶占未知工作。
新協定無法攔截擁有 Docker 管理權限的人手動啟動其他 GPU 容器；遷移仍需 owner 窗口。

## 身分與狀態

- Asset ref：所有必要本機檔案的完整 SHA256／size manifest hash。註冊及 load 前完整
  逐 block 重讀；拒絕缺檔、混入逃逸 symlink、非法 shard path、hash mismatch。
  size/mtime 或小檔 hash 不取代完整權重驗證。現有 `modelctl register` 的歷史輕量
  inventory 不會自動變成此模式的完整驗證。
- Deployment id：typed model/revision/asset ref、immutable image ID 或 registry digest、
  runtime/adapter version、量化、完整 load/resource config 的 canonical SHA256。
  不同 context、capacity、dtype、revision、runtime 或 host memory 設定都不同。
- Job id：穩定資源；execution attempt 是另外的隨機 id，附 controller fencing epoch
  和 worker epoch。單次已建立 dispatch intent 的工作不自動建立第二 attempt。
- 三種資源分開：model lease（載入／暖機／常駐）、request lease（CPU preparation＋
  GPU execution/drain）、asset pin（禁止回收使用中資產）。

模型：`unloaded → loading → warming → ready → draining → unloading → unloaded`。
load/warmup/transport/unload 無法確認時進 `unknown`；只有 request terminal 或可確認
整個受管 engine/container 退出才解除 GPU 使用權／asset pin。僅 frontend epoch
改變、RPC 回應接受取消、HTTP 斷線、kill 命令送出都不是退出證據。
完整 asset／image／mount／network 準備在持久化外部啟動 intent 之前完成，且不能啟動
GPU；這段已知未啟動的失敗或 crash 會回到 unloaded 並 block deployment。已有 start
intent 後的失敗仍是 unknown，不能僅憑找不到 container 就推定沒有啟動過。

工作：`queued → dispatching → running`，執行逾時後 `draining`。terminal 後的
`succeeded/failed/canceled` 是運算狀態，另有 `result_state=none/pending/available/
storage_failed`。`expired` 是 queue deadline；失敗依賴傳遞 `dependency_failed`。
只在所有依賴運算成功且成果 available 後才 eligible。取消 queued 不 dispatch；
unknown 依賴保持等待直到結果恢復或自身 queue deadline，不把不確定推定成失敗。
取消 running 記錄 cancel_requested，繼續追蹤至 terminal，不能立即釋放名額。

## 排程與 admission

每個安全名額重新選擇：eligible urgent 依提交 sequence FIFO；沒有 urgent 才選 normal。
urgent 不搶占已執行、load、warmup 或 drain；在下一個邊界重新選擇，不保留尚未執行
normal 的優先權。連續 urgent 可延後 normal，aging 不凌駕 urgent。

Normal 優先同個已載 deployment，最多 `reuse_dispatches` 次連續 dispatch（預設 3，
**是工作 dispatch 次數，不是批次**），或最舊 eligible normal 已等 `aging_seconds`
（預設 60 秒）即選最舊。模型卸載後重置計數。capacity=1 的固定例子：Qwen ①、
Whisper ②、同 deployment Qwen ③，執行 ①③②。

同部署依 manifest capacity 共用名額：legacy 占一個／capacity=2 時，同部署 urgent
可使用另一名額。跨部署 urgent 到達就 seal admission，停止補 normal，等全部 lease
終止再切換。舊 Chat/SSE 送 worker 前也在同一 SQLite transaction 取得 lease；已有
eligible durable 工作時拒絕新 legacy admission（429），避免無界 legacy 流量飢餓工作。

## API

全部 `/v1` routes 使用原有 public Bearer；unknown 欄位／型別拒絕，不靜默忽略。
Job 提交、查詢、取消、catalog、artifact upload 在模型切換時仍可用。
`GET /v1/models` 只列實際 ready 的 Qwen 或 Whisper，否則 503；catalog 是註冊清單。
`/health/ready` 的 inflight/detached/uncertain 来自共同 store，不能只計 API 本機 task。

| Route | 輸入／結果 |
|---|---|
| `GET /v1/catalog` | 已註冊 deployment，沒有本機資產 path／secret |
| `GET /v1/scheduler` | phase、deployment、epoch、heartbeat、inflight |
| `POST /v1/artifacts` | `{media_type:"audio/wav",data_base64,description?}`，回 artifact_ref |
| `GET /v1/artifacts/{ref}` | `{media_type,data_base64}` |
| `POST /v1/jobs` | 必填 `Idempotency-Key`，新 job 201、既有同內容 200、不同內容 409 |
| `GET /v1/jobs?after=0&limit=100` | 按 sequence 分頁，上限 100 |
| `GET /v1/jobs/{id}` | 運算／成果狀態、attempt、error code、result_description；不附敏感輸入 |
| `POST /v1/jobs/{id}/cancel` | 嚴格空 JSON `{}`，可重複取消 |
| `GET /v1/jobs/{id}/result` | available 時回 JSON；尚未保存 409 |

```json
{
  "deployment_id": "dep_<64 hex>",
  "operation": "chat",
  "input": {
    "model": "Qwen/Qwen3.5-4B",
    "messages": [{"role": "user", "content": "Reply OK."}],
    "max_tokens": 32
  },
  "urgent": false,
  "depends_on": [],
  "queue_timeout_seconds": 3600,
  "execution_timeout_seconds": 60,
  "result_description": "optional local result description"
}
```

`result_description` 是每個 job 的可選 metadata，在 submit/get/list 可查回；不送入
模型、不修改成果 bytes。同內容結果可共用 artifact_ref，但各 job 的描述不互相覆蓋。

`operation=transcribe` 的 input 是 `{audio_ref,language?,max_tokens?}`；語言支援
`en/zh/ja/ko/de/fr/es` 或省略自動辨識。僅 canonical 44-byte RIFF/WAV、PCM16、
16 kHz、mono、0<duration≤30 秒、≤1 MiB；拒絕多餘 chunks、truncated/trailing bytes。
max_tokens 1–256／預設 128。無翻譯、timestamp、diarization、long-form、任意 prompt
或 ASR SSE。耗盡 token 回 `finish_reason=length,truncated=true`，不是完整轉錄。

Async chat 支援既有 typed Chat 子集，`stream=false`；SSE 仍走原 Chat route。
urgent／dependency／timeout 是排程 metadata，不能流入 upstream generation payload。
400/401/429/503/504、原 SSE error 且不補 `[DONE]` 的語義保留；Whisper 不接受 Chat。

## 時間、容量與保存

| 上限 | 預設／意義 |
|---|---|
| queue_timeout_seconds | 3600 秒，submit 到 dispatch 的 wall-clock deadline（包含等待 load） |
| execution_timeout_seconds | 60 秒，dispatch 到運算等待上限；必須 ≤ drain_seconds |
| client transport timeout | client 自己的等待，與持久工作運算生命週期無關 |
| load_seconds / warmup_seconds | 各 180 秒，獨立預算，逾時不推定停止 |
| drain_seconds / unload_seconds | 600／60 秒；到期 unknown，不釋放 GPU |
| queue_capacity / retained_jobs | 256／4096，transaction 中核對 |
| storage_bytes | 1 GiB，input/artifact/receipt/reservation；實際 orphan bytes 也計入 |
| input_bytes / result_bytes | 24 MiB／2 MiB；artifact WAV 另限 1 MiB |
| retention_seconds | 7 天；只回收已結束且成果 none/available、沒有依賴引用者 |
| max_load_attempts / max_generation_attempts | 10000／100000，持久化累計硬上限；驗收 state 應調低 |

設定由 `scheduler init --config` 的嚴格 SchedulerConfig 建立並持久化，其他程序不得
以不同設定打開同 state。queue/input/result/event 數量皆有界。沒有無限隊列、任意
script、training/backtest、checkpoint resume 或雲端 storage/execution provider。

Load/warmup 失敗會把該 deployment 標為 blocked，尚未 dispatch 的相關 jobs 明確
failed，不在 engine 退出後無限自動重載。只有管理者在 engine 已退出後使用
`scheduler resume-deployment <dep-id>` 解除，再以新 job 明確重試。Load budget 在外部
load 前持久化，並保守預留該 profile 的完整 warmup 生成次數上界；job／legacy admission
各占一次 generation budget，即使取消發生在 GPU dispatch 前也不退還。這是執行次數
上界，不是成功次數。達上限不再 dispatch；重新啟動不重置計數。

正式長期 state 可使用管理端 `scheduler budget-window open --name <唯一名稱> --loads N
--generations N` 加上臨時驗收上限。所有 load/warmup、durable job 與 legacy admission
在同一交易中同時檢查 lifetime 與臨時上限。`budget-window status` 顯示累計及窗口歷史。
驗收結束後 `budget-window close --name <名稱>` 只解除臨時限制，不重設 lifetime；窗口
結束計數固定保留且名稱不可重用。開關都要求 phase unloaded/ready、無 lease 與待執行
工作；engine 退出已證實的歷史 unknown outcome 保留且不重派，不阻止關閉窗口。

SQLite WAL + synchronous FULL + BEGIN IMMEDIATE 是同機 admission 權威。內容 canonical
hash 配 unique idempotency key 處理並發與回應遺失；在 retention 刪除 job 前 key 有效。
建立 durable dispatch intent／lease 後才呼叫 worker，該切點前後失聯可能留下 unknown；
不宣稱任意故障 exactly-once，不因 engine 後來退出而重新生成未知 attempt。

結果先寫 immutable fsync/readback receipt，再提交 terminal，再發布 content-addressed
artifact。檔案 replace 或 DB commit 失敗都保留 receipt／reservation，`recover-storage`
只重試保存。若連 receipt 都寫不出，活著的 controller 保留唯一記憶體結果並停止新
dispatch；這時若程序也崩潰，沒有持久 bytes 可救回，狀態維持 unknown/unavailable。
GC 先提交 tombstone，再核對無引用才刪檔；pending／unknown／唯一未發布 receipt 不能清。

## 管理與使用

在同機 Linux／WSL 的 native filesystem 選獨立 state，勿使用現役 static `.state`：

```bash
uv run --locked modelctl --state ~/.local/state/selfhost-scheduler scheduler init
uv run --locked modelctl --state ~/.local/state/selfhost-scheduler scheduler asset-register --path /absolute/verified/model
uv run --locked modelctl --state ~/.local/state/selfhost-scheduler scheduler register --file deployment.json
# 只有取得切換窗口、舊 static owner 正常停機後，才可執行：
uv run --locked modelctl --state ~/.local/state/selfhost-scheduler scheduler controller
uv run --locked modelctl --state ~/.local/state/selfhost-scheduler scheduler api --port 18081
```

`deployment.json` 完整欄位以 `Deployment` schema 為準；image 可為本機 `sha256:<image id>`
或 `repository@sha256:<registry digest>`，啟動 `--pull=never`，不 build/download/fallback。
必須由這版 Dockerfile 建置包含 internal key middleware／relay 的候選 image；既有
static release image 未包含這些能力，不能直接冒充 managed image。Image 建置本身
不會啟動 GPU；建置與 GPU 窗口依各自授權執行，不以這段範例當部署授權。
Qwen resource 預設保留 shm 2 GiB、無新增 host RAM limit；Whisper 必須明確 RAM≤8 GiB、
capacity=1、FP16/SDPA。所有變更影響 deployment id。
Worker 的 PID 上限也包含 Linux threads；預設為256，relay仍為32。
固定 vLLM 的 API／engine 與多組 NCCL/Gloo 初始化已在128上限重現thread建立失敗。
已註冊的128設定不會自動改寫；調整後須註冊新的deployment並使用新ID。
下方Whisper範例的128是明確override；Qwen的256設定仍須在目標主機通過GPU驗收。

以下可作最小 manifest；**先替換 asset_ref 與 image 的全零占位值**。asset_ref 取
`asset-register` 回傳，image 取已建置 image 的 `docker image inspect --format '{{.Id}}'
<image-tag>`，不需要 push。Qwen 與 Whisper 使用不同檔案，分別 register：

```json
{
  "model": "Qwen/Qwen3.5-4B",
  "revision": "851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a",
  "asset_ref": "asset_0000000000000000000000000000000000000000000000000000000000000000",
  "runtime": "vllm",
  "runtime_version": "vllm-0.29.0",
  "image": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "load": {"dtype": "bfloat16", "attention": "runtime", "context": 8192,
           "capacity": 2, "video": true, "gpu_memory": 0.6,
           "host_memory_gib": null, "shm_gib": 2, "cpus": 4, "pids": 256}
}
```

```json
{
  "model": "openai/whisper-small",
  "revision": "973afd24965f72e36ca33b3055d56a652f456b4d",
  "asset_ref": "asset_0000000000000000000000000000000000000000000000000000000000000000",
  "runtime": "whisper",
  "runtime_version": "transformers-5.16.1",
  "image": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "load": {"dtype": "float16", "attention": "sdpa", "capacity": 1,
           "gpu_memory": 0.17, "host_memory_gib": 8, "shm_gib": 1,
           "cpus": 4, "pids": 128}
}
```

Whisper 的 0.17 是 PyTorch allocator fraction 設定，不是總 VRAM 硬體隔離；驗收仍需
監測實際 VRAM。CPU relay 無 GPU、另限 256 MiB／1 CPU。管理端 `status` 顯示累計
load/generation budget，`events --after <seq> --limit 100` 匯出不含 payload 的時間事件。

Windows／WSL client 使用明確 public key file（Windows 可讀 WSL key 的安全檔案來源），
不複製到 code/log，也不向 client 提供 internal worker key：

```bash
uv run --locked modelctl scheduler catalog --api-key-file <public-key-file>
uv run --locked modelctl scheduler upload --api-key-file <public-key-file> --file synthetic.wav
uv run --locked modelctl scheduler submit --api-key-file <public-key-file> --file job.json --idempotency-key example-1 --urgent
uv run --locked modelctl scheduler get --api-key-file <public-key-file> job_<id>
uv run --locked modelctl scheduler result --api-key-file <public-key-file> job_<id>
```

停止 controller 不等於停止 engine；它保留 lease/pin。`scheduler unload` 是管理端的
明確受控停機／恢復入口，需要 controller OS lock；核對自身容器 labels、停止並確認
整個 engine 退出後，才解除 gate。無法取得可信退出證據時維持 unknown，交管理者處理，
不得刪 journal／queue／volume metadata 規避。`recover-storage`、`collect` 為管理 CLI，
不提供 public recovery／GC endpoint。啟動時 full asset hash 驗證與 warmup 不下载模型。

## 驗證邊界

CPU tests、真 CPU subprocess、無 GPU Docker relay，以及 fixed-runtime CPU processor
各自有獨立證據，不能取代 GPU load/switch/VRAM/resource-release 驗收。Whisper checkpoint
為 `openai/whisper-small@973afd24965f72e36ca33b3055d56a652f456b4d`，官方 safetensors
966995080 bytes，完整 SHA256
`1d7734884874f1a1513ed9aa760a4f8e97aaa02fd6d93a3a85d27b2ae9ca596b`。
它是排程整合候選；中文辨識與產品品質仍未驗證。

GPU 驗收 D 必須另外取得 fresh owner 釋放與窗口，核對實際運行 image/source/revision、
完整資產、lease、host/WSL/VRAM，不以 checkout、HTTP 200、舊研究窗口截止或 CPU 測試
推定 GPU 可用。測試矩陣與當次結果見 [acceptance](acceptance.md)。
