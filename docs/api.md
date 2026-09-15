# API 0.1 支援範圍

這是 OpenAI-compatible Chat Completions 的明確子集。**不是完整 OpenAI 相容實作。** 未列參數及巢狀欄位一律 400，不靜默忽略。

## Endpoints

- `GET /health/live`：API process 存活。
- `GET /health/ready`：200 表示目前 worker 已成功暖機；否則 503。含 inflight、detached、uncertain 計數與 worker epoch。
- `GET /v1/models`：Bearer 驗證；只列目前指定且 ready 的模型及固定 revision。
- `POST /v1/chat/completions`：Bearer + application/json，一般 JSON 或 SSE。

每次產生新的 `X-Request-ID`，回 `X-Service-Version`；不採信 client 自訂 ID。可提供 `X-Request-Timeout-Ms` 正整數縮短 deadline（1ms 至部署上限），不得延長。

## Chat 欄位

| 欄位 | 範圍／預設 |
|---|---|
| model | 精確 HF repo ID；需與目前部署相同 |
| messages | 1–64 則，system/user/assistant/tool；純文字每則至多 32,768 字元 |
| stream | boolean，預設 false |
| max_tokens | 整數 1–1024，預設 128；總 token context 另外由 vLLM 模型設定檢查 |
| temperature | 0–2，預設 1 |
| top_p | (0,1]，預設 1 |
| seed | 可選 0–2^32-1；不保證跨 GPU/runtime 完全一致 |
| stop | 可選非空字串或 1–4 個非空字串，每個至多 200 字元 |
| presence_penalty / frequency_penalty | -2 至 2，預設 0 |
| stream_options | 僅 stream=true；只支援 include_usage boolean |
| tools | 支援 profile 限 function tool，1–8 個；name、description、parameters JSON schema |
| tool_choice | 有 tools 時可選 auto 或 none；不支援 required／強制指定 function |
| chat_template_kwargs | Qwen3.5 profile 限 enable_thinking boolean，省略預設 false |

尚不支援 n、logprobs、logit_bias、response_format、JSON schema constrained decoding、parallel_tool_calls 開關、audio/video、embedding、Responses API、LoRA、自動選模。工具參數 schema 會傳給模型 template 作描述，不提供 strict schema 執行保證；客戶端必須驗證輸出的工具名稱／JSON 及自身權限。

## 模態與工具

- text profile：文字 chat。
- qwen3_5 profile：文字、單張圖片、function calling；qwen3 reasoning parser + qwen3_coder tool parser。
- gemma4 profile：配置已保留文字、單圖及 gemma4 tool parser；**只有實際驗收報告中的 profile 可視為已驗證**。
- 僅 user content 可為 text／image_url parts。圖片必須是 inline `data:image/png;base64,...`，PNG 格式、解碼後 ≤1 MiB、長寬均 ≤1024。每次最多一張。URL、file path、其他格式會拒絕，不讓服務代抓任意網址。
- assistant tool_calls 使用 id/type/function{name,arguments}；tool message 必須帶匹配的 tool_call_id。所有待處理 call 要先有結果才能继续生成。
- 服務只輸出 tool_calls；工具實際執行與多輪 agent loop 由產品或驗收 client 負責。

## 錯誤與串流

401 未授權；400 不合法／不支援參數、模型 context 超限；404 未服務模型／route；413 body 過大；415 格式錯；408 body 接收過慢；429 超載；503 worker 不 ready／未知工作未清除；504 deadline；502 worker 執行／協定無法確認。

錯誤包含 `error.message/type/code/request_id`，不附回原始 prompt 或完整 worker exception。429/503 有 Retry-After。HTTP SSE 開始後錯誤無法改 status；改送 `data: {"error":...}` 並結束，不送 `[DONE]`。客戶端應同時檢查 error 與 `[DONE]`，不可將中途斷線當成功。

deadline 與取消不是立即 GPU abort；完整語義見 architecture.md。
