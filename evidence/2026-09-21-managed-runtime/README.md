# Managed GPU 驗收停止與原服務恢復

本輪 managed GPU 驗收已 **STOP**，完整 D 未完成，正式 managed 服務未交付。
原靜態 Qwen 已恢復，Windows 合成文字推論及跨工具回合持續服務驗證通過。
四次 managed Qwen load 均未達 ready；驗收 state 累計 **4 loads／24次暖機上界預留**，
durable compute attempts=0。24是已扣除且不退還的預留額度，並非完成24次生成。
能力、132順序、urgent、cancel、capacity、restart、ASR與正式 managed smoke均未覆蓋。
第四次初始化逾時的根因仍為 **UNKNOWN**。

## 四次載入與修正

| Load | 已知結果 | 修正與證據界線 |
|---|---|---|
| 1 | relay使用不存在的python，OCI start失敗；worker後續exit1 | 改python3；另以CPU證實vLLM cache寫入唯讀home失敗，轉至有界runtime cache |
| 2 | FlashInfer獨立workspace仍寫入唯讀home，worker exit1 | 指定workspace；同image的CPU模型類別import通過，CUDA未初始化 |
| 3 | Triton .so在runtime-cache無法映射，worker exit1 | CPU對照證實實際mount含noexec；加入explicit exec，保留1GiB上限及其他限制 |
| 4 | distributed_init／NCCL日誌後未完成啟動，達180秒上限 | 根因UNKNOWN；main停止managed，受控退出後恢復原static |

第四次exact worker在2026-09-21T03:28:10.678603195Z確認exited、Pid=0、
ExitCode=137、OOMKilled=false；不能把137稱為OOM。relay有exact Docker
die(exit0)→destroy事件。退出後state=unloaded、lease=0、model pins=0、
engine cleanup=0，ownership gate正常釋放。第一次保留Docker die(exit1)／destroy
與store事件；第2至4次另保留獨立whole-container exit inspect。
四個failed/canceled jobs及原ledger保留，沒有重送未知compute attempt。

修正提交為ea342fd（relay與cache）、18e9ccd（FlashInfer）、47af794
（runtime-cache exec、CUDA cache及TMPDIR）。59項相關CPU測試在ea342fd通過，
後兩次各10項定點測試通過；實際provider argv的無GPU容器也驗證兩個UID的cache可寫
與native .so載入，/tmp仍noexec。收尾在47af794程式碼上另驗證scheduler boundaries、
process及bounded-command共17項，6.18秒通過。這些CPU結果不能代替GPU驗收。

兩個候選image以本機快取離線建置，worker source仍為固定bfc1f6b元件；
host controller runtime另有47af794固定release，兩者不可混為同一版本。
Qwen CPU probe通過；Whisper首次fixture import path有誤，同image加入
PYTHONPATH=/app後通過。保留首次失敗。沒有下載、替換runtime套件、切換PyTorch
或擴大tmpfs。原static成功跨過distributed initialization，但image、rootfs、
CPU/PID限制、cache及network均有差異，不能據此定位managed根因。

## 窗口與預算

窗口始於2026-09-21 11:02:17 Taipei，12:02:17停新案例，12:32:17為原交付／恢復截止，
沒有延長。總額最初12 loads／64 generations；未使用修復1／6經main批准轉回驗收，
使同native state上限7／38→8／44，原2／12計數不變。其後使用者明確批准增加6
generations，總額12／70、驗收8／50，原3／18計數不變。兩次維護均保存SQLite
backup、完整其他table hashes、舊events及獨立批准receipt。

最後保守占用 **6 loads／37 generations**：初始Desktop start UNKNOWN 1／6、
managed 4／24、原static rollback 1／6、Windows文字smoke 0／1。暖機以原上界全額
計入，不退額。main批准的smoke 1次取自未使用正式2／8預留，正式分配降為2／7，
總額仍12／70。正式native state實際0／0，臨時window已正常關閉並保留原8次上限歷史。
剩餘額度不代表新GPU授權；managed已STOP。

window ledger第二筆allocation history的prior欄位誤含更新後8／50及70；獨立
budget-user-plus6-receipt才是8／44→8／50、64→70的正確前後證據。原ledger保存於
rollback證據，另附更正receipt；不覆寫原始歷史或重設計數。

## 原 static 恢復

單次modelctl restart於11:31:45 Taipei執行；11:33:59 Windows ready200/true。
沿用原兩個container IDs、image IDs、模型、key、Compose及state；無build/pull/recreate。
模型為Qwen/Qwen3.5-4B、revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a、
vLLM、context8192、capacity2、video capability=true。

11:34:43的單次Windows合成文字請求在0.25秒回覆OK，finish_reason=stop，usage為
19 prompt／2 completion tokens。請求完成後ready=true，inflight/detached/uncertain
全零，原lease journal空值hash相同。這是文字推論與傳輸證據，不是產品品質驗收，
也未重新測試image/video/tools。

11:36:06–07在client及本task監看程序均退出後，另一工具回合再次確認Windows ready、
模型metadata及lease全零；exact原container IDs/images/mounts仍一致，只有原兩個
static容器運行，ownership屬static。原static透過Docker Desktop持續服務，沒有必要
新增WSL keepalive；未測Windows reboot，不宣稱開機自啟。

## 保留的證據與限制

原始啟動logs、容器事件、DB、每秒VRAM／Windows與WSL RAM樣本及private receipts
保留於ignored state；公開JSON只列必要結果與SHA256，沒有key或完整inspect。
第一版Windows monitor因atomic rename的PermissionError停止，guard偵測stale；
當時controller已inactive，沒有在量測缺口啟動新GPU。第二版加入有界rename重試，
新樣本與舊失敗分開保留。Windows metadata length滯後另以直接讀bytes確認。

temporary guard已disarm且退出，observer及第二版resource monitor正常退出。
候選images、四份固定native releases、兩份native states與inactive且disabled的驗收
units保留供診斷；沒有刪除DB、jobs或模型。原static持續運行；其他應用未被本次清理停止。
沿用feat/single-host-scheduler及唯一主要worktree，沒有新增worktree。
無merge、push、registry發布或managed正式交付；未知根因需下一個明確授權階段處理。
