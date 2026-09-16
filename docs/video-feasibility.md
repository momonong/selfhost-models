# 影片輸入可行性與決策（2026-09-16）

起點 `main@5a077e3883b3b2b6a0e817ff91f209ace6b15bd9`，單一工作目錄，工作分支 `feat/video-inference`。
產品回合切分、精彩程度 prompt／評分與人工內容驗收不在本 repo。

## 已核對的固定配套

- Qwen/Qwen3.5-4B revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，本機唯讀資產。
- vLLM 0.29.0、Transformers 5.16.1、V1 runner；官方 runtime 未更換。
- 模型 `video_preprocessor_config.json` 使用 `Qwen3VLVideoProcessor`，patch 16、temporal patch 2、spatial merge 2；template 有 video placeholder。
- 既有 worker `launch.py` 明確設 `video:0`，不能把模型支援宣稱成目前 API 已支援。
- 固定 vLLM `VideoMediaIO.load_base64` 支援內部 `data:video/jpeg;base64,<JPEG>,...`，metadata 接受 `fps`、`frames_indices`、`total_num_frames`、`duration`、`do_sample_frames:false`。
- vLLM Qwen3VL processor 以相鄰兩幀時間戳的平均值建立 temporal patch 時間標記，文字格式精度一位小數。實際原始採樣時間戳仍需另回傳，不能把 patch 平均時間稱作逐幀時間。

官方參考：[固定模型 revision](https://huggingface.co/Qwen/Qwen3.5-4B/tree/851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a)、[vLLM 影片輸入](https://docs.vllm.ai/en/stable/features/multimodal_inputs/)。版本細節以本機固定容器原始碼與實測為準。

## Processor CPU 實測

[原始結果](../evidence/2026-09-16-video/processor.json)。合成紅、綠、藍畫面，已知 30fps 時軸，均勻取樣含首尾；這一輪直接建立預取樣畫面，**尚未驗證 MP4 解碼或 GPU 生成**。

| 時長 | 幀數 | 每幀尺寸 | 輸入 token（含本次合成問題） |
|---|---:|---|---:|
| 2s | 8 | 256×256 | 309 |
| 10s | 16 | 256×256 | 597 |
| 30s | 16 | 256×256 | 602 |
| 60s | 16 | 256×256 | 604 |
| 60s | 32 | 256×256 | 1186 |
| 60s | 16 | 384×384 | 1244 |
| 60s | 120 | 256×256 | 4391 |

2048 context 可容納稀疏 16／32 幀，但 60s、約 2fps、120 幀需要更大 context；已在維護時段實測 8192 context、原 GPU memory fraction 0.60，最終可行結果見下節。模型原始影片長度與推論 token 成本並非線性對應；固定幀數主要付出時間資訊損失，較高解析度主要增加每個 temporal patch 的視覺 token。

## 真實 GPU 決策與保留的失敗

三輪原始證據分別保留，沒有以成功覆蓋失敗：

1. [原圖片像素預算](../evidence/2026-09-16-video/gpu-probe.json)：120 幀被 processor 自動降解析度，僅 796 input tokens；不能算 256² 成功。
2. [請求完整像素預算](../evidence/2026-09-16-video/gpu-full-resolution.json)：30 秒／60 幀成功；60 秒／120 幀被 2048-token encoder cache 拒絕，400，沒有 CUDA OOM。
3. 巢狀 videos_kwargs.size 造成固定 runtime 重複傳入 size，啟動失敗；[完整日誌](../evidence/2026-09-16-video/nested-processor-startup.json)。未 patch upstream 或更換 runtime。
4. [最終 encoder/cache 配置](../evidence/2026-09-16-video/gpu-sized-cache.json)：平面 max_pixels=7,864,320，8192 context、8192 max_num_batched_tokens、兩個名額、GPU 0.60，全部完成。

| 合成來源時長 | 送入幀數／尺寸 | prompt tokens | worker request 秒 | nvidia-smi 取樣峰值 |
|---|---|---:|---:|---:|
| 2s | 4 / 256² | 170 | 1.857 | 16,059 MiB |
| 10s | 20 / 256² | 746 | 3.559 | 16,059 MiB |
| 30s | 60 / 256² | 2,206 | 4.014 | 16,063 MiB |
| 60s | 120 / 256² | 4,396 | 5.562 | 16,063 MiB |
| 60s | 32 / 256² | 1,191 | 4.219 | 16,063 MiB |
| 60s | 16 / 384² | 1,249 | 4.267 | 16,063 MiB |

以上是單次 sequential worker 探測，不含 MP4 接收／解碼；wall time 包括 worker HTTP、prefill 與最多 96 output tokens，30s/60s 的 finish_reason=length，不能當完整回答。VRAM 是 host 全域 nvidia-smi 約 250ms 間隔取樣，含常駐 vLLM pool 與其他 host 使用，不是 request allocator peak 或準確增量歸因。

2 秒只有 4 幀時漏答藍色；60 秒稀疏 16/32 幀的色段邊界有顯著錯誤。120 幀對合成紅→綠→藍的順序與大約 20/40 秒轉換有可用回覆，但**只支持合成粗粒度時間資訊**。沒有桌球真實片段或人工精彩程度評估。

決定採用單段 inline MP4/H.264、1–60 秒、16 MiB、CFR ≤60fps、1280×720、最多3600來源幀；約2fps均勻取樣含首尾、最多120幀、256²黑邊補圖。明確 opt-in，只對固定 vLLM/Qwen3.5-4B 開放；Transformers 不支援。完整欄位、錯誤、timestamp 與資源規則以 [API](api.md)／[backend](backend.md) 為準。

## MP4 CPU 探測

使用 uv 的一次性 `av==16.0.1` 工具環境，此初次探測沒有修改 production 依賴或 image；實作階段才把同版 PyAV 納入 uv.lock。
[初次結果](../evidence/2026-09-16-video/decode-probe.json)、[含候選最高來源尺寸的結果](../evidence/2026-09-16-video/decode-stress.json)。
合成檔僅位於 ignored `.state/video-fixtures/`，沒有下載外部素材。

| H.264 MP4 | 來源幀數 | 原始檔大小 | 完整解碼時間 |
|---|---:|---:|---:|
| 2s / 640×360 / 30fps | 60 | 36,142 bytes | 約 0.015s |
| 10s / 640×360 / 30fps | 300 | 158,948 bytes | 約 0.031s |
| 30s / 640×360 / 30fps | 900 | 477,199 bytes | 約 0.078s |
| 60s / 640×360 / 30fps | 1,800 | 959,526 bytes | 約 0.156s |
| 60s / 1280×720 / 60fps | 3,600 | 1,720,914 bytes | 約 0.813s |

驗證每個解碼幀的 PTS 與 CFR 時軸相符、完整幀數與容器宣告一致，採樣時間戳另存於 JSON。這是 Windows CPU 上、低複雜度合成素材的單次探測，不代表實拍素材效能。其後 [Linux 有界 decoder](../evidence/2026-09-16-video/linux-decoder.json) 對 60s/720p60 完成解碼＋120 幀 JPEG 準備（2.187 秒），真實子程序 timeout/cancel 後 kill、wait、無剩餘 child、暫存為空；不是 native decoder 漏洞抵禦證明。

## 重跑與品質邊界

```powershell
uv run --locked python scripts/video_feasibility.py --output evidence/<new-run>/processor.json
uv run --locked python scripts/video_decode_probe.py --stress --output evidence/<new-run>/decode.json
```

此命令只在現有 worker 中載入 processor，沒有載入第二份模型。腳本另有 GPU 維護模式，但必須先協調排他使用時段、啟用實驗 worker 的影片能力並確認沒有 client 請求；它直連內部 worker，只用於能力探測，不是 API admission 驗收。

原 baseline CPU 契約 87 passed；最終 API、CPU 與故障驗收另見 [驗收文件](acceptance.md)。沒有提供已授權桌球片段；即使合成順序與時間回答成功，也不代表快速攻防理解或精彩程度判斷品質通過。
