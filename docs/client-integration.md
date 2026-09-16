# Client 接入指南

本指南供其他專案的開發者與 coding agent 接入已由 `selfhost-models` 啟動的本機模型服務。它說明接入流程、能力選擇、失敗語義與產品責任；完整欄位限制仍以 [API 0.1 支援範圍](api.md) 為準，部署與維運由服務擁有者依 [本機使用指南](local-use.md) 及 [部署文件](deployment.md) 處理。

## 接入前提與設定

接入方不負責啟動、重啟、切換 backend、下載模型或修改 Compose。開始前向服務擁有者確認：

1. Docker Desktop 與模型服務正在運行，而且 `GET /health/ready` 回 200、`ready` 為 `true`。
2. 取得可連線的 base URL；目前已驗證的同機預設是 `http://127.0.0.1:18080`。OpenAI SDK 若需要 `/v1` base URL，才使用 `http://127.0.0.1:18080/v1`。
3. 取得 API key 的安全來源。預設 key 由 `modelctl serve` 首次建立於 `D:\projects\selfhost-models\.state\api-key`；只讀取內容，不把 key 複製到原始碼、Git、issue、聊天、測試 fixture、錯誤訊息或日誌。
4. 有權讀取 key 的程序才可接入。Windows 依 `.state` 目錄既有 ACL，Linux key file 應為 mode 600；需要更嚴格權限時由服務擁有者調整。

以下是**接入專案可自行採用的設定名稱**，不是服務會讀取或保證支援的環境變數：

| 建議名稱 | 意義 | 建議值／規則 |
|---|---|---|
| `SELFHOST_MODEL_BASE_URL` | API origin，不含 `/v1` | 同機已驗證值 `http://127.0.0.1:18080` |
| `SELFHOST_MODEL_API_KEY_FILE` | key file 路徑 | 本機開發可指向服務 repo 的 `.state/api-key`；不要提交內容 |
| `SELFHOST_MODEL_API_KEY` | 由 secret store 注入的 key | 只在不能使用 key file 時採用；不得設成非秘密的預設值 |
| `SELFHOST_MODEL_EXPECTED_ID` | 可選的模型 allowlist／部署防呆 | 範例為 `Qwen/Qwen3.5-4B`；實際值仍由 `/v1/models` 確認 |

產品可改用自身命名或 secret manager。服務實際接受的是 URL、`Authorization: Bearer <key>`、JSON request 與少數 HTTP headers；它不會讀取上表名稱。範例中的模型、30 秒服務 deadline、並行數與 backend 都是目前部署範例，不是跨部署保證。

## 最小接入流程

### 1. 檢查 live 與 ready

- `GET /health/live`：只表示 API process 存活，不表示模型可用。
- `GET /health/ready`：不需認證；只有 HTTP 200 且 `ready: true` 才可送新生成請求。HTTP 503 表示不可用。
- ready response 的 `inflight` 是尚未終止的持久 lease 數；`detached` 是 client 已離開但 gateway 仍在追蹤的工作；`uncertain` 是 API 無法確認結果的歷史工作。任一異常都應交由服務擁有者診斷，不刪 journal、不自行重啟。

健康檢查可用較短 timeout，但不要把一次 probe 失敗直接解讀為模型、GPU 或 Docker 已停止。

### 2. 查詢模型與能力

以 Bearer key 呼叫 `GET /v1/models`。目前一次只服務一個模型；接入方應取 `data[0]`，並核對：

- `id`：原樣放入 chat request 的 `model`，不要自行縮寫或自動選另一模型。
- `revision`、`backend`：可記錄為診斷 metadata；不要據此推測未宣告能力。
- `capabilities`：每次程序啟動、ready epoch 變更或收到能力相關 400 後重新查詢。
- `max_inflight`：部署容量資訊，不是鼓勵 client 同時填滿容量；產品仍應有自己的有界併發。

功能只在對應 capability 為 `true` 時啟用：`text`、`stream`、`images`、`tools`、`thinking`。`queue_capacity` 目前為 0；服務不替產品排無界工作。`cancellation: "drain_to_terminal"` 表示取消 client 等待後，底層工作仍可能繼續。`unsupported_parameters` 是 backend 額外禁止的欄位清單，但不是完整 API schema；完整限制仍看 [API 文件](api.md)。

### 3. 呼叫 Chat Completions

使用 `POST /v1/chat/completions`、`Content-Type: application/json` 與 Bearer key。這是 OpenAI Chat Completions 的嚴格子集，**不是完整 OpenAI API 相容層**：未知欄位與不支援參數會回 400，不會靜默忽略。已支援欄位、範圍與預設值見 [Chat 欄位](api.md#chat-欄位)；沒有 Responses API、embeddings、audio/video、JSON schema constrained decoding、LoRA 或自動選模。

一般回應是單一 JSON。SSE request 設定 `stream: true`；逐行處理 `data:` frame，收到 `{"error": ...}` 即失敗，只有收到 `data: [DONE]` 才算完整成功。HTTP 200、已顯示部分文字或 socket 正常關閉都不能取代 `[DONE]`。若使用 `stream_options: {"include_usage": true}`，仍須容許只有部分 chunk 帶 usage。

每個 response 都有服務產生的 `X-Request-ID` 與 `X-Service-Version`；記錄它們可協助診斷，但不要記錄 prompt、完整 response、key 或圖片。接入方可用 `X-Request-Timeout-Ms` 將服務 deadline 縮短到 1ms 至部署上限，不能延長；client transport timeout 應略大於服務 deadline，讓服務有機會回傳明確錯誤。

## Backend 與能力差異

接入方不能要求服務自動 fallback。服務擁有者選定 vLLM 或 Transformers 後，`/v1/models` 會宣告目前實際能力：

| 能力 | vLLM `qwen3_5` | Transformers `qwen3_5` |
|---|---|---|
| 文字、一般 JSON、SSE、usage | 支援 | 支援 |
| 圖片 | 支援；仍須看 `capabilities.images` | 不支援 |
| function tools／tool history | 支援；仍須看 `capabilities.tools` | 不支援 |
| `chat_template_kwargs.enable_thinking` | 支援；預設 false | 只接受 false，true 會拒絕 |
| `stop` | 支援 | 不支援 |
| 非零 presence／frequency penalty | 支援 | 不支援 |
| 容量 | 部署決定，目前上限宣告為 16 | 固定單請求、queue 0 |

Transformers 在 `temperature: 0` 時要求 `top_p: 1`。不要把同一 payload 假設成可跨 backend 使用；先查 capability，再依 backend 的拒絕規則組裝 request。`gemma4` profile 只有在當次實際 `/v1/models` 宣告且驗收證據涵蓋時才可使用，不能因程式保留設定就視為已驗證。

## 圖片與工具

圖片只允許放在 `user` message 的 content parts：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "請描述圖片。"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,<BASE64>"}}
  ]
}
```

每個 request 最多一張 inline PNG；解碼後最多 1 MiB，長寬各不超過 1024。HTTP URL、file path、非 PNG 與格式錯誤的 data URI 都會拒絕；服務不代抓外部內容。接入方應先在產品邊界檢查大小、格式、來源授權與敏感資訊。

工具只支援 `type: "function"`，每次 1–8 個。模型回傳的 `message.tool_calls[]` 使用 `id`、`type` 與 `function: {name, arguments}`；`arguments` 是字串，必須由產品解析 JSON、依 allowlist 核對工具名稱與 schema、執行權限檢查，再自行執行。服務不執行工具，也不保證 arguments 符合 schema。

送回結果時，保留原 assistant `tool_calls`，再加入 `{"role":"tool", "tool_call_id":"<matching id>", "content":"<result>"}`。所有 pending call 都須有匹配結果才能繼續。外部副作用、去重鍵、審批、sandbox、timeout 與 audit log 均由產品負責。已用合成資料驗證一般 JSON 的單次工具呼叫及結果回送；不要把它延伸成 agent 品質或任意工具安全性的證據。

## 錯誤、取消與重試

先讀 HTTP status，再讀 `error.code`；SSE 開始後則檢查 error frame。錯誤 body 不會回顯原始 prompt。建議策略如下：

| 情況 | 是否自動重試 | 原因／處理 |
|---|---|---|
| 400、401、404、413、415 | 否 | 修正 payload、認證、模型或 route；原樣重送不會成功 |
| HTTP 408 body timeout | 最多有限次，且只重建尚未送入生成的 request | 一般發生在 request body admission 前；先排除網路或 body 問題 |
| 429 `overloaded`／`too_many_clients` | 可有限次 | 尚未 admission；遵守 `Retry-After`，加 jitter、總次數與總時限，並保留產品端 backpressure |
| 503 `worker_not_ready` | 可在重新通過 ready 後有限次 | 該次未 dispatch；不要持續輪詢生成 endpoint |
| 其他 502／503、504 | 否 | 可能已 dispatch 而結果未知；自動重送可能造成重複工作 |
| client timeout、取消、連線中斷、SSE error／缺 `[DONE]` | 否 | 都不能證明 GPU 已停止或工作未完成；將結果標為未知或失敗並保留 request ID |

取消只停止產品等待或傳輸，**不代表 GPU 已停止**。Gateway 會保留 lease 並 drain 到 backend 的 terminal confirmation；期間可能持續佔用容量。不得用立即重送、無界 exponential retry、切換 backend 或刪除 state 來「恢復」。若業務操作有副作用，產品應在模型呼叫之外使用 idempotency key／工作 ID；服務的 request ID 只供此服務診斷，不是產品去重保證。

SSE 中斷後，不要把已顯示文字提交成完整 assistant message，也不要自動把相同 conversation 再送一次。由產品決定讓使用者明確重試、建立新工作，或在確認沒有外部副作用後重做。

## 不同執行位置的連線

| Client 位置 | 位址與狀態 |
|---|---|
| 同一台 Windows 主機上的程式 | `http://127.0.0.1:18080`；已用目前筆電、Docker Desktop WSL2 與真實 GPU 服務驗證 |
| 同一台筆電的 Ubuntu WSL | `http://127.0.0.1:18080`；已用 Linux Python client 做合成文字呼叫驗證，仍屬同一台 Windows 筆電的證據 |
| 其他 Docker 容器 | 未驗證；容器內 `localhost` 是該容器，不能當成 Windows host。不要自行假設 `host.docker.internal`、加入共用 network 或改 Compose |
| 遠端主機、LAN、公網 | 未開放、未驗證；目前 Compose 只 publish 到 host `127.0.0.1`。不要改成 `0.0.0.0`、加 port forward／tunnel／proxy 來繞過邊界 |
| Linux 實體桌機 | 部署流程已有文件，但實機接入仍未驗證；完成 [Linux 桌機驗收](acceptance.md#linux-桌機實機驗收待執行) 前不可標為已支援環境 |

若其他容器或遠端產品確實需要接入，應另立部署／安全任務，明確設計認證、TLS、網路 ACL、secret distribution、rate limit、觀測與驗收；這不是 client 更換 hostname 就能完成的事項。

## 可執行範例

### Python：一般與 SSE（只用標準函式庫）

從接入專案執行。先設定 `SELFHOST_MODEL_API_KEY_FILE`；未設定時，範例只為同機開發讀取服務 repo 的預設 key 路徑。

```python
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

base = os.getenv("SELFHOST_MODEL_BASE_URL", "http://127.0.0.1:18080").rstrip("/")
key_file = Path(os.getenv(
    "SELFHOST_MODEL_API_KEY_FILE",
    r"D:\projects\selfhost-models\.state\api-key",
))
key = os.getenv("SELFHOST_MODEL_API_KEY") or key_file.read_text(encoding="utf-8").strip()

def get_json(path, authenticated=False):
    headers = {"Authorization": f"Bearer {key}"} if authenticated else {}
    with urlopen(Request(base + path, headers=headers), timeout=5) as response:
        return json.load(response)

ready = get_json("/health/ready")
if ready.get("ready") is not True:
    raise RuntimeError("model service is not ready")
model = get_json("/v1/models", authenticated=True)["data"][0]
if not model["capabilities"]["text"]:
    raise RuntimeError("deployment does not advertise text chat")

payload = {
    "model": model["id"],
    "messages": [{"role": "user", "content": "Reply with exactly: READY"}],
    "temperature": 0,
    "max_tokens": 16,
}

def post(stream=False):
    body = {**payload, "stream": stream}
    request = Request(
        base + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    return urlopen(request, timeout=35)

with post() as response:
    result = json.load(response)
    print(result["choices"][0]["message"]["content"])

if not model["capabilities"]["stream"]:
    raise RuntimeError("deployment does not advertise streaming")
done = False
with post(stream=True) as response:
    for raw in response:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            done = True
            break
        chunk = json.loads(data)
        if "error" in chunk:
            raise RuntimeError(f"stream failed: {chunk['error']['code']}")
        for choice in chunk.get("choices", []):
            print(choice.get("delta", {}).get("content") or "", end="", flush=True)
if not done:
    raise RuntimeError("stream ended without [DONE]; partial output is not success")
print()
```

### curl：模型查詢、一般與 SSE

以下為 Bash／WSL 範例；key 只從 file 讀入 shell 變數，輸出與日誌中不要加 `set -x`。PowerShell 可採相同 request，但應使用 `curl.exe`，避免舊版 `curl` alias 差異。

```bash
BASE_URL="${SELFHOST_MODEL_BASE_URL:-http://127.0.0.1:18080}"
KEY_FILE="${SELFHOST_MODEL_API_KEY_FILE:-/mnt/d/projects/selfhost-models/.state/api-key}"
KEY="$(<"$KEY_FILE")"

curl --silent --show-error --fail-with-body "$BASE_URL/health/ready"
curl --silent --show-error --fail-with-body --config - "$BASE_URL/v1/models" <<EOF
header = "Authorization: Bearer $KEY"
EOF

# 將 model 換成上一個 response 的 data[0].id。
curl --silent --show-error --fail-with-body --config - \
  --json '{"model":"Qwen/Qwen3.5-4B","messages":[{"role":"user","content":"Reply with exactly: READY"}],"temperature":0,"max_tokens":16}' \
  "$BASE_URL/v1/chat/completions" <<EOF
header = "Authorization: Bearer $KEY"
EOF

# --no-buffer 立即輸出 SSE；成功必須看見 data: [DONE]。
curl --silent --show-error --fail-with-body --no-buffer --config - \
  --json '{"model":"Qwen/Qwen3.5-4B","messages":[{"role":"user","content":"Reply with exactly: READY"}],"temperature":0,"max_tokens":16,"stream":true}' \
  "$BASE_URL/v1/chat/completions" <<EOF
header = "Authorization: Bearer $KEY"
EOF
unset KEY
```

curl 範例便於人工 smoke，不會替你解析 SSE error 或驗證 `[DONE]`；產品 client 必須實作這兩項檢查。不要把真實 key、prompt 或 response 保存為 CI log。

### 範例驗證層級

- 上述 Python code block 已在 2026-09-16 對本機 `vLLM`／`Qwen/Qwen3.5-4B` 的 ready 服務原樣執行；一般與 SSE 都以合成文字回覆 `READY`，SSE 收到 `[DONE]`，呼叫後 lease 計數回到 0。這只證明 client 流程與當次服務契約相容，不是模型品質驗收。
- curl 的 models、一般與 SSE request 已用同機 `curl.exe` 等價執行並收到一般 `READY` 與 SSE `[DONE]`；文件中的 Bash 變數與 heredoc 包裝只做靜態核對，未把它宣稱為本次 WSL shell 實測。
- 圖片與工具格式依 schema、能力驗證程式及既有 [合成 GPU smoke 證據](../evidence/2026-09-16-local-use/vllm-smoke.json) 核對；本指南撰寫時未重新送圖片或執行工具。
- 錯誤、取消、deadline、lease 與 backend 差異依 API／schema／capability／gateway 實作和契約測試核對；本指南撰寫時未做超載或故障注入。可重跑範圍與歷史真 GPU 證據見 [驗收文件](acceptance.md)。

## 產品責任邊界與驗證

`selfhost-models` 負責固定模型／runtime 的推論服務、輸入驗證、能力宣告、有界 admission、request ID，以及保守追蹤 deadline／取消後的 GPU 工作。接入產品仍負責：

- system／business prompt、對話歷史裁切、資料最小化、個資與授權；
- capability negotiation、client timeout、有界併發、退避、未知結果 UX 與產品級 idempotency；
- 工具 allowlist、arguments schema 驗證、權限、實際執行、結果清理與 agent loop；
- 模型輸出的事實查核、安全政策、人工校正，以及針對真實使用者與資料的品質／效益驗收；
- 不把 health、合成 smoke、HTTP 200 或模型自行陳述視為產品品質證據。

接入驗證至少包含：ready 200、帶認證的 `/v1/models`、能力核對、合成文字一般回應、合成文字 SSE 且收到 `[DONE]`，以及錯誤 key 確認 401。圖片與工具只有產品確實使用、且當次 capability 宣告支援時才驗證。不要在接入測試中重啟／切換 backend、執行故障注入、填滿容量或使用真實私人資料。

## 可複製到其他 repo 的任務開場

```text
請依 D:\projects\selfhost-models\docs\client-integration.md 接入本機模型服務，不修改 selfhost-models 的服務、模型、部署或 backend。

產品端請使用可配置的 base URL 與 secret；同機已驗證預設為 http://127.0.0.1:18080，API key 從 D:\projects\selfhost-models\.state\api-key 安全讀取，不寫入原始碼、Git 或日誌。先檢查 GET /health/ready，再以 Bearer key 查 GET /v1/models，使用回傳的 model id、backend 與 capabilities 決定功能。只接入文件列出的 OpenAI Chat Completions 子集；不要假設 Responses API、完整 OpenAI 相容、自動 fallback、遠端／LAN 或其他容器連線。

實作一般 JSON 與 SSE chat；SSE 必須檢查 error frame 及 [DONE]。遵守圖片與 function tool 格式；工具只由產品端驗證與執行。對 502／504、取消、連線中斷或不完整串流不得自動重送，因為取消不代表 GPU 已停止；429／明確未 dispatch 的 503 也只能依 Retry-After 做有界重試。

完成時以合成、非敏感文字驗證 ready、models、一般回應、SSE 完整結束與錯誤 key 401；回報實測環境、request ID、能力快照及未驗證項目。不要重啟服務、切 backend、做故障注入或宣稱產品品質已驗證。
```
