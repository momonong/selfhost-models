# API 0.1 支援範圍

這是 OpenAI-compatible Chat Completions 的明確子集。**不是完整 OpenAI 相容實作。** 未列參數及巢狀欄位一律 400，不靜默忽略。其他 repo 的完整設定、能力選擇、重試與連線指引見 [Client 接入指南](client-integration.md)；本頁保留協定欄位的權威規格。

## Endpoints

- `GET /health/live`：API process 存活。
- `GET /health/ready`：200 表示目前 worker 已成功暖機；否則 503。含 inflight、detached、uncertain 計數與 worker epoch。
- `GET /v1/models`：Bearer 驗證；列目前 ready 的模型、固定 revision、backend、capabilities 與 max_inflight。能力依實際部署宣告。
- `POST /v1/chat/completions`：Bearer + application/json，一般 JSON 或 SSE。

每次產生新的 `X-Request-ID`，回 `X-Service-Version`；不採信 client 自訂 ID。可提供 `X-Request-Timeout-Ms` 正整數縮短 deadline（1ms 至部署上限），不得延長。

## Chat 欄位

| 欄位 | 範圍／預設 |
|---|---|
| model | 精確 HF repo ID；需與目前部署相同 |
| messages | 1–64 則，system/user/assistant/tool；純文字每則至多 32,768 字元 |
| stream | boolean，預設 false |
| max_tokens | 整數 1–1024，預設 128；總 token context 另外由選定 backend 檢查 |
| temperature | 0–2，預設 1 |
| top_p | (0,1]，預設 1 |
| seed | 可選 0–2^32-1；不保證跨 GPU/runtime 完全一致 |
| stop | 可選非空字串或 1–4 個非空字串，每個至多 200 字元 |
| presence_penalty / frequency_penalty | -2 至 2，預設 0 |
| stream_options | 僅 stream=true；只支援 include_usage boolean |
| tools | 支援 profile 限 function tool，1–8 個；name、description、parameters JSON schema |
| tool_choice | 有 tools 時可選 auto 或 none；不支援 required／強制指定 function |
| chat_template_kwargs | Qwen3.5 profile 限 enable_thinking boolean，省略預設 false |

尚不支援 n、logprobs、logit_bias、response_format、JSON schema constrained decoding、parallel_tool_calls 開關、audio、embedding、Responses API、LoRA、自動選模。工具參數 schema 會傳給模型 template 作描述，不提供 strict schema 執行保證；客戶端必須驗證輸出的工具名稱／JSON 及自身權限。

## 模態與工具

下列多模態／工具 profile 屬 vLLM。Transformers 的 `qwen3_5` 目前只支援文字、一般／SSE 與 usage，單請求、queue 0，thinking 固定 false。不支援的圖片、tools/tool history、thinking=true、stop、非零 presence/frequency penalties 於 dispatch 前回 400；零 penalties 與顯式 thinking=false 可接受。`temperature=0` 時 `top_p` 必須為 1；非零 temperature 使用 top_p 取樣且不套用隱藏的 top_k 預設。seed 不保證跨 runtime/GPU 一致。

- text profile：文字 chat。
- qwen3_5 profile：文字、單張圖片、function calling；qwen3 reasoning parser + qwen3_coder tool parser。
- gemma4 profile：配置已保留文字、單圖及 gemma4 tool parser；**只有實際驗收報告中的 profile 可視為已驗證**。
- 僅 user content 可為 text／image_url parts（影片部署另允許 video_url，見下節）。圖片必須是 inline `data:image/png;base64,...`，PNG 格式、解碼後 ≤1 MiB、長寬均 ≤1024。每次最多一張。URL、file path、其他格式會拒絕，不讓服務代抓任意網址。
- assistant tool_calls 使用 id/type/function{name,arguments}；tool message 必須帶匹配的 tool_call_id。所有待處理 call 要先有結果才能继续生成。
- 服務只輸出 tool_calls；工具實際執行與多輪 agent loop 由產品或驗收 client 負責。

## 有界影片（明確 opt-in，vLLM Qwen3.5-4B）

只有 `/v1/models` 的 `capabilities.videos=true` 才可使用。部署命令為 `modelctl serve Qwen/Qwen3.5-4B --video --context 8192`；固定 revision 為 `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，vLLM 0.29.0，最多 2 個 inflight。預設未啟用；Transformers、gemma4 均不支援、不 fallback。模型清單另回 `max_model_len`、`max_body_bytes` 及 `capabilities.video_limits`。

仍使用 `POST /v1/chat/completions`，user content 加入：

```json
{"type":"video_url","video_url":{"url":"data:video/mp4;base64,<BASE64>"}}
```

每個 request 最多一段，與圖片互斥；只接受 inline MP4/H.264。拒絕 HTTP URL、file URL、host path、外送的 JPEG sequence、未知欄位、client 自選 `media_io_kwargs`／`mm_processor_kwargs`。不提供持久上傳物件或音訊能力。

| 邊界 | 規則 |
|---|---|
| 檔案 | 原始 bytes ≤16 MiB；MP4 `ftyp` brand 為 isom/iso2/iso4/iso5/iso6/mp41/mp42/avc1 |
| 視訊 | 恰好一個 H.264 track；1–60 秒；CFR、已知 fps/幀數/PTS；≤60fps、≤3600 幀 |
| 原始尺寸 | width ≤1280、height ≤720；固定尺寸；拒絕 rotation metadata 與 VFR |
| 音軌 | 可以存在，但不解碼、不傳給模型，`audio_processed=false` |
| 取樣 | `min(來源幀數, 120, 2×ceil(時長))`，向下取偶數；在 `[0, N-1]` 等距取 ordinal，Python round ties-to-even；包含首尾 |
| 處理後畫面 | 保持比例縮放、黑邊補成 256×256，JPEG quality 85；worker 不再取樣；影片總 pixel budget 7,864,320 |
| 接收／解碼 | 單 JSON body ≤24 MiB、接收 ≤10 秒且不超過 client deadline；32 handlers、全體正在接收的 body 合計 ≤64 MiB；解碼必須先取得 durable lease |
| 解碼子程序 | Linux；512 MiB address space、10 CPU 秒（11 秒 hard limit）、15 秒 wall time、8 MiB 輸出、≤7201 demux packets；累計解碼像素 ≤3,317,760,000 |
| Context | 8192，包括模板、文字、視覺、時間標記及 max_tokens；超過仍拒絕，不裁剪 prompt 或偷偷降低取樣 |

時間戳以第一個實際解碼 video frame 為 0。一般 JSON 在頂層 `video` 回傳 duration、source dimensions/fps/frame count、`frame_indices`、實際 `timestamps_seconds`、sample count、resize/sampling 規則及音軌旗標。SSE 在第一個模型 chunk 附加同一個 `video` 欄位，保留原 id/model/created/choices，之後沿用模型 chunk／usage／`[DONE]`。收到 metadata 不表示生成成功。

Qwen processor 將每兩個幀合成一個 temporal patch；`model_patch_timestamps_seconds` 是兩個 ordinal/fps 的平均，模型模板再以一位小數表示。這和逐幀 PTS、精確事件邊界不同。2fps／256² 會丟失快速動作與小物體細節，不能當成桌球攻防或精彩度準確性保證。

格式／codec／時間軸／來源限制：400 `invalid_or_unsupported_video`；base64：400 `invalid_video_base64`；解碼 byte 超限：400 `video_too_large`；子程序異常／OS budget：400 `video_decode_limit`；解碼 wall timeout：504 `video_decode_timeout`；接收合計超限：429 `receive_budget_exceeded`。Schema 拒絕仍為 400 `invalid_request`，HTTP body 超限為 413。未 ready／超載在解碼前拒絕。解碼期間 deadline／取消會保留 lease 至子程序結束與檔案清理，之後不 dispatch GPU；GPU 已 dispatch 則沿用 drain-to-terminal。

## 錯誤與串流

401 未授權；400 不合法／不支援參數、模型 context 超限；404 未服務模型／route；413 body 過大；415 格式錯；408 body 接收過慢；429 超載；503 worker 不 ready／未知工作未清除；504 deadline；502 worker 執行／協定無法確認。

錯誤包含 `error.message/type/code/request_id`，不附回原始 prompt 或完整 worker exception。429/503 有 Retry-After。HTTP SSE 開始後錯誤無法改 status；改送 `data: {"error":...}` 並結束，不送 `[DONE]`。客戶端應同時檢查 error 與 `[DONE]`，不可將中途斷線當成功。

deadline 與取消不是立即 GPU abort；完整語義見 architecture.md。

## 最小 client（從 repo 根目錄執行）

```python
from pathlib import Path
import httpx

key = Path('.state/api-key').read_text().strip()
with httpx.Client(base_url='http://127.0.0.1:18080',
                  headers={'Authorization': f'Bearer {key}'}, timeout=35) as client:
    payload = {'model': 'Qwen/Qwen3.5-4B',
               'messages': [{'role': 'user', 'content': 'Reply OK.'}],
               'max_tokens': 32, 'temperature': 0}
    response = client.post('/v1/chat/completions', json=payload)
    response.raise_for_status()
    print(response.json()['choices'][0]['message']['content'])
```

SSE 將 payload 加上 `stream: true`，使用 `client.stream(...)` 與 `response.iter_lines()` 讀取；應驗證結尾 `[DONE]` 並處理 error frame。圖片及完整工具往返 client 見 `scripts/multimodal_smoke.py`。
