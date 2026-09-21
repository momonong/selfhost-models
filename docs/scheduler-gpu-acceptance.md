# 單機排程 D：GPU 驗收與 managed 交付窗口

**2026-09-21 本輪 managed 驗收已 STOP；完整 D 未完成，正式 managed 未交付。**
四次 Qwen load 均未達 ready；驗收 state 累計4 loads／24次暖機上界預留，
durable compute attempts=0。第四次初始化逾時的根因仍為 UNKNOWN。
原 static Qwen 已於11:33:59 Taipei恢復ready；Windows單次合成文字推論與
client／临時監看程序退出後的跨工具回合存活檢查通過，lease全零。
最後保守占用6 loads／37 generations，未合併或推送。
退出證據、CPU 修正及原 static 恢復結果見 `evidence/2026-09-21-managed-runtime/`。
下列案例表保留為原定驗收規格，不構成繼續執行或增加額度的授權。2026-09-20
授權包含必要修正、本機建置、managed 部署與真 GPU 驗收；成功後留下持續服務。
此文件不是 RESOURCE GO，不授權大型下載、推送、發布、並載模型、清除未知 lease
或無界重試。現役 owner、外部 writers 與維護截止時刻須在操作前確認。

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

窗口最長 **90 分鐘**；T+60 停止新驗收，最後 **30 分鐘**只做交付或恢復。
Deadline 取「第一次 GPU lifecycle 操作起90分鐘」與「已批准 owner 維護截止時刻」
較早者。Desktop restart 可能自動 reload 原 Qwen，因此需要恢復時從 restart 前計時。
GO 前準備不占 GPU 窗口；不可因晚取得 GO 延长 owner 釋放時刻。

原總上限64次generation；使用者本輪另明確批准增加6次，現在總上限 **70 次 generation、12 次 load attempt**，包含暖機、legacy route、失敗、
取消、未知結果與恢復暖機；不是僅計成功 HTTP。Health/models/query 不算 generation。
dispatch/reserve 後不退額度，unknown 不重送。每次執行之前同時核對持久 budget 和
窗口 ledger；所有 client requests 由唯一驗收 owner 控制。

全局 ledger 串起各個位置，不可只計 managed DB；最後分配如下：

| 分配 | load 上限 | generation 上限 |
|---|---:|---:|
| 初始 Desktop start UNKNOWN（保守保留） | 1 | 6 |
| 獨立驗收 state | 8 | 50 |
| 正式 state 未用預留（已转出1次smoke） | 2 | 7 |
| 原 static rollback 保留 | 1 | 6 |
| 原 static Windows文字smoke | 0 | 1 |
| **總計** | **12** | **70** |

9/21 曾為定點 runtime 修復預留1／6、將驗收縮至7／38。該修復預留完全未執行，
main 於本輪明確批准將其轉回驗收8／44；總額12／64與初始UNKNOWN1／6不變。
同一native state在API/controller停止、unloaded/lease0後，以SQLite backup、
BEGIN IMMEDIATE與完整資料hash核對，精確將config7／38改為8／44；budget2／12、
failed/canceled jobs、pins和原events保持不變，另追加operator reallocation事件。
不得把這次明確批准的配置轉換視為日常可自行放寬上限的操作。

第三次配置失敗後，使用者另明確批准總generation64→70；以相同受控方式將同state
8／44→8／50，原budget3／18與所有歷史保留，不改load上限與截止時間。

驗收 state 設 `max_load_attempts=8,max_generation_attempts=50`。原規格的正式 state 保留長期
10000／100000上限，以 `scheduler budget-window open --name final-handoff --loads 2
--generations 8` 增加臨時限制；不得修改 DB 或重設歷史計數。正式 Qwen 的6次暖機、
Windows durable smoke一次、legacy smoke一次合計8；第二次 load 不代表可以超出8次。
初始start UNKNOWN依1／6全額占用，不因後來唯讀觀察無GPU而退額。
本輪正式state沒有開始服務，budget=0／0，原2／8臨時window已以正常close命令關閉，
保留其歷史上限與事件。main另批准從未用正式預留轉1次generation給原static文字smoke；
最後占用為initial UNKNOWN1／6＋managed4／24＋rollback1／6＋smoke0／1＝6／37。
只有授權開始前已 ready 的歷史 load 不重算。未耗 reserve 不用來新增案例；已證實
未觸發的釋放記入 ledger。rollback 額度提前保留。
第四次失敗後，已用24次暖機上界預留。原定完整序列另需14次暖機與18個必要案例，
合計56，超過驗收上限50；main 已停止 managed，本輪不再啟動 GPU 案例。
能力、132順序、urgent、cancel、capacity、restart、ASR與正式 managed smoke
全部未覆蓋；啟動失敗不能計為能力案例通過。取消兩個optional案例，不另加重試。
deployment 失敗後 blocked，不自動 resume/retry；未知結果不重送。

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

## 原定工作與額度（本輪未執行成功）

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
| 本輪選做餘額 | 0 | 兩個optional案例取消，所有已用預留保留 |
| **必要測試合計** | **18** | **另加14次暖機；第四次失敗後總需求為56，超過50，已STOP** |

如果模型太快而未真正觀察到 running/cancel/overlap，不把該次當作該邊界通過；只可在
剩餘額度內補測，否則標示未覆蓋。5xx、OOM、未知結果、load/warmup 失敗或 engine
identity 不一致即停新工作，進入受控恢復；不靠新增重試填滿成功數。

## 成功交付與狀態交接

1. 停驗收新提交、保存證據並停止驗收 controller。使用該 state 的 `scheduler unload`
   證實 worker 全退出、釋放 ownership gate；驗收 DB 原樣保留。
2. 正式 API/controller/SQLite 使用獨立 native WSL state 與固定 source/venv；不複製
   驗收 DB 或 static `.state/runtime`。安全複製原 public key，Windows 繼續讀原 key file。
3. systemd 分開監督 API/controller，加 Hidden Windows WSL keepalive；驗證跨工具回合
   與 client 結束後仍服務。未測 Windows reboot，不宣稱開機自啟。重啟 active 不等於
   ready；保留 unknown 時必須診斷與受控 unload，不自動 adopt 或清 lease。
4. 正式 origin 固定 `http://127.0.0.1:18080`，衝突回報。用實際 catalog deployment IDs
   完成合成 submit/get/result/cancel、legacy smoke，最後 Qwen ready、lease=0。
   Whisper 由 durable job 觸發切換；Whisper ready 時 legacy Chat 明確拒絕，不 fallback。
   要切回 Qwen，提交 catalog 中 Qwen deployment 的 durable job。
5. 正式 state 無 queued/active lease 且 phase ready 後，以 `budget-window close` 固定
   窗口歷史、解除臨時上限，lifetime 累計保留；核對服務不受已耗盡驗收額度阻擋。
   unknown outcome 保留且不重派；只有 engine 退出已證實、lease解除才可關閉窗口。

## 失敗時恢復原 Qwen

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
   本輪另明確批准1次合成文字smoke，已完成；不因此擴張成重測所有能力。

若 engine 退出不能證實、gate不能安全釋放、原 Qwen 恢復失敗或達全局 deadline，停止操作並
立即回 orchestrate/main，列明現役狀態與已用額度；不再啟動另一 engine，不宣稱恢復。

## 交付證據

每個 case 保存合成輸入 hash、job/attempt/deployment/worker/controller epoch、事件
sequence、queue/load/warmup/compute/result 時間、call/load ledger、VRAM peak與退出
證據。另交 managed 持續服務或失敗時原 Qwen 恢復核對。逐項分為 PASS／FAIL／未覆蓋；真 GPU 流程通過仍不代表
中文 ASR、影片內容理解或任何產品品質已驗收。
