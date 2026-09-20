# 單機排程 D：待核定的 GPU 驗收窗口

**狀態：提案，未執行。** A/B/C CPU 證據不構成此窗口授權。指定 owner、開始時間、
現役使用者釋放、恢復責任與以下上限須由 main 核定後才開始。此文件不授權下載、
變更 release tag、並載模型、清除未知 lease 或故障後無界重試。

## 前置檢查

- 完成 A/B/C 核對；候選 Qwen/Whisper image 與本輪 source manifest 一致。所有 image
  和模型已在本機，`--pull=never`，runtime 啟動不得下載。Qwen 固定 revision
  `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`、vLLM 0.29.0；Whisper 固定 revision
  `973afd24965f72e36ca33b3055d56a652f456b4d`、Transformers 5.16.1。
- Fresh owner 回覆沒有外部推論使用者，依序讀 ready → authenticated models → lease；
  盤點實際 API/worker image ID、runtime、mounts、Compose 設定 hash、GPU/host/WSL RAM。
  公開結果不保存 key、完整 inspect 或主機個人路徑。
- 保存原 static 容器識別與復原命令；保留原 image、模型與 state。原 API/worker
  若不符合已知 Qwen context8192/capacity2/video/gpu0.60 設定，停止並先核對。
- 確認 native Linux state、controller/API 唯一程序、測試 ports 可用；完整資產 hash
  預先驗證。只有停止原 static owner、確認其整個 engine 已退出並重新量測資源後，
  managed controller 才取得同 daemon ownership gate。

## 時間與硬上限

窗口最長 **60 分鐘**；T+45 分鐘停止新測試，保留最後 **15 分鐘**恢復原 Qwen。
T+15 尚未取得第一個 managed Qwen ready，或 T+30 尚未取得 Whisper ready，停止後續
測試、保存診斷並開始恢復。不可因進度不足延長窗口。

總上限 **40 次 generation、8 次 load attempt**，包含暖機、legacy route、失敗、
取消、未知結果與恢復暖機；不是僅計成功 HTTP。Health/models/query 不算 generation。
dispatch/reserve 後不退額度，unknown 不重送。每次執行之前同時核對持久 budget 和
窗口 ledger；所有 client requests 由唯一驗收 owner 控制。

獨立驗收 state 設定 `max_load_attempts=7,max_generation_attempts=34`，另保留原 static
Qwen 恢復 **1 次 load＋6 次暖機 generation**；因此兩模式合計最多 8／40。預定僅用
4 次 managed load（Qwen→Whisper→Qwen→Whisper）＋1 次原 Qwen 恢復。
兩次 managed Qwen 暖機各6次、兩次 Whisper 各1次，合計14；原 Qwen 恢復6次。
因此預定20次暖機，測試最多20次。若故障多耗一次 load/warmup，須從剩餘測試額度扣除，
不能突破總上限。失敗後 deployment blocked；不自動 resume/retry。

測試採固定合成文字、單色 PNG、合成工具回傳、2 秒 H.264 clip，以及 1／10／30 秒
PCM16/16kHz/mono 的靜音或音調 WAV；不用私人音訊。Chat/ASR max_tokens≤128；音訊
≤30秒/1MiB，影片不擴大既有 public 上限。Whisper capacity1、FP16/SDPA、RAM8GiB、
GPU allocator fraction0.17；Qwen context8192/capacity2/video、BF16、GPU0.60，沿原
shm2GiB、不新增未驗證 RAM limit。每個 GPU worker CPU quota4、pids128；CPU relay
另限1CPU/256MiB。這些不是 GPU 品質或資源足夠的保證。

開始與執行中 Windows、WSL 可用 RAM 各至少8GiB；整張 GPU 使用達22528MiB（22GiB）、
OOM、host pressure、driver/daemon 異常時停止新 dispatch。每秒記錄 host-wide VRAM
與記憶體，保留量測時間。load/warmup 各180秒、drain120秒、unload60秒；到期不推定
GPU 停止，保留 unknown 和退出證据要求。

## 預定工作與額度

| 項目 | 最多 generation | 檢查 |
|---|---:|---|
| 第一個 Qwen 的 JSON text/image、工具兩回合、video、正常 SSE | 6 | 能力、usage/terminal、影片 metadata、SSE DONE；工具由合成 client 執行 |
| Qwen SSE 斷線 | 1 | detach 後 lease 保留到 terminal，無假 DONE |
| 同 Qwen capacity2 與 legacy/durable 共用 | 2 | 最多兩個真執行，額外 admission 429 不生成 |
| normal Qwen①、Whisper②、同部署 Qwen③ | 3 | 固定場景①③②；load/deployment/event 可追溯 |
| Whisper running cancel＋client 不再等待 | 1 | cancel_requested 保留 ownership 到 terminal；不得立即卸載 |
| 兩個 urgent Qwen＋一個 normal Qwen | 3 | urgent FIFO 在安全邊界先 dispatch，normal 不搶先，不中斷在跑工作 |
| Qwen 工作 dispatch 後 controller restart／受控 engine 退出 | 1 | 舊 epoch fenced、unknown 不重派、whole-container exit 前 pin/lease不釋放 |
| 恢復到第二個 Whisper 的有界 transcribe | 1 | 只有退出證實後切換，result bytes可取、description可查 |
| 有界餘額 | 2 | 只補既有案例的未覆蓋長度／邊界；事前登記，不用來改驗收標準 |
| **測試合計** | **20** | **另加預定20次暖機，總計40** |

如果模型太快而未真正觀察到 running/cancel/overlap，不把該次當作該邊界通過；只可在
剩餘額度內補測，否則標示未覆蓋。5xx、OOM、未知結果、load/warmup 失敗或 engine
identity 不一致即停新工作，進入受控恢復；不靠新增重試填滿成功數。

## 恢復原 Qwen

1. 禁止新 client submit；記錄最後 budget/events/jobs/result 狀態，停止 controller
   （僅停止 controller 不代表 worker 退出）。
2. 用此獨立 state 的 `scheduler unload` 取得 controller lock，核對受管容器 labels，
   停止 worker/relay並 inspect 整體退出；由 store 解除資源，再釋放 ownership gate。
   未知 attempt 不重新生成，不刪 SQLite／receipt／pin／gate 來繞過保護。
3. 只在上述退出證據成立後，使用原 static state 的 `modelctl restart`，沿用原來
   已存在的 Compose 容器與 image；不 build、pull、recreate 或修改原 Compose 設定。
   新 static CLI 會核對共同 gate。恢復只預留一次 load與既定6次暖機。
4. 依序核對 ready、authenticated models、實際原 image ID/revision/context/capacity/
   video、lease全零與記憶體；保存恢復前後差異。保留 managed 測試 state 供追溯。

若 engine 退出不能證實、gate不能安全釋放、原 Qwen 恢復失敗或到 T+60，停止操作並
立即回 orchestrate/main，列明現役狀態與已用額度；不再啟動另一 engine，不宣稱恢復。

## 交付證據

每個 case 保存合成輸入 hash、job/attempt/deployment/worker/controller epoch、事件
sequence、queue/load/warmup/compute/result 時間、call/load ledger、VRAM peak與退出
證據。另交原 Qwen 恢復核對。逐項分為 PASS／FAIL／未覆蓋；真 GPU 流程通過仍不代表
中文 ASR、影片內容理解或任何產品品質已驗收。
