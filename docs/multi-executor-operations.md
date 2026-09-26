# 單機完善與多執行端架構驗證

狀態：本階段工程實作與單GPU驗證完成；兩CPU執行端用於架構證據，不等同多GPU部署。起點 `10b95fb42ef5dbf91c699a31124a65f594871722`，
由 main2 交付既有 task。沿用 checkout、新工作分支；允許本機提交及受控驗收部署，
原工程階段未合併、推送或發布 image。後續 0.3.0rc1 發布見 [版本紀錄](releases/0.3.0rc1.md)；沒有新增 worktree。

## 已確認目標

- 權威 jobs/attempts/leases/results、排程與資源授權在控制端 SQLite；每個 executor 有
  獨立身分、credential、模型清冊、engine/fence/journal，不能讀控制 DB。
- 工作只匹配完整 deployment ID，維持 urgent FIFO、有限模型親和性、aging及依賴。
  可執行工作不受不匹配或離線 worker 的隊首工作阻塞；未知 attempt 不跨 worker 重派。
- 至少兩個真正獨立 CPU fixture executor，透過 HTTP 與獨立檔案空間驗證並行、異質
  能力/資源、忙碌/離線、回覆遺失、重啟、過期命令及結果恢復；不能冒稱多GPU證據。
- 單張4090仍由唯一實際GPU executor取得daemon-wide ownership gate；不以別名分裂資源。
- 持久原子維護開關分為停止新收件、停止dispatch／保留佇列、排空後卸載；可操作全域或
  單一worker。未知工作與lease不隱含取消/釋放，單worker維護不停止其他worker。
- executor安全清理只處理已確認結果、已ACK且無輸出讀者/引用的terminal資料。
  unknown與未確認結果不得回收，去重身分保留；提供保留量、可回收量、剩餘容量及滿載原因。
  去重metadata有界滿載時保守拒絕，不宣稱無限運行，不清理歷史驗收raw。
- 公開單executor Chat/SSE/jobs與deployment ID相容；多worker管理視圖另增，不將部分ready
  說成全體ready或將單worker失敗說成API死亡。重大公開契約變更回main。

## 實作選擇

沿用既有 Controller/RemoteProvider 狀態機，以同一控制DB中的每worker狀態/授權視圖隔離
生命週期、lease和失敗；多worker協調器並行推進獨立worker。可解釋的排序為既有工作優先級，
再按完整deployment匹配、可用/維護狀態、已載入親和性及固定worker ID tie-break。queued
規劃指派可在未建立attempt時解除；dispatch intent後只查原executor，不重派。

ACK與可清理分開：durable結果已落控制receipt可釋出executor副本；legacy spool要等原
API producer已讀完或API重啟確認原reader已不存在才釋出，controller的terminal對帳不能
搶先刪仍在讀的stream。GC交易保留ID/hash/fence/grant/terminal摘要作tombstone；舊命令
返回已收集狀態而不是重新執行。SQLite頁面重用與WAL checkpoint另行回報。

## 驗收與資源

先locked CPU與多程序/檔案隔離，再保存當次主機盤點、精確候選、有限時間/load/generation
新窗口與回復預留，才操作本專案GPU。保留原artifact/state/模型/歷史raw，migration只在
備份副本；候選通過才留服務，否則安全回復。驗證Qwen/Whisper切換、legacy/durable共享
admission、cancel/detach、maintenance、receipt/GC組合，提供等待/執行/load時間觀測，
不自創效能門檻。不重啟主機、登出、enable自啟/linger、修改driver/全域Docker或其他服務。

## 未來項目／非目標

真多主機/多GPU、LAN/公網、其他OS、專案權重/公平份額/搶占、多租戶RBAC、訓練與
checkpoint、分散式單工作、新模型、雲端/付費、Kubernetes/broker/新DB不在本輪。

## 操作介面

所有範例在repo根目錄，`CONTROL_STATE`、credential檔與executor URL由操作者依部署設定。
API key與內部credential不同，均只從檔案讀取。登錄操作需要API/controller停止；啟用fleet
還要求原引擎已unloaded、沒有lease/model pin/待清理engine，且無未保存的local-runtime結果。
原始state先備份；升級只操作備份或獨立候選，不直接用舊版開啟已遷移資料庫。

```bash
uv run --locked modelctl --state "$CONTROL_STATE" scheduler fleet-enable
uv run --locked modelctl --state "$CONTROL_STATE" scheduler executor-add \
  --authority "$AUTHORITY_ID" --executor "$EXECUTOR_ID" --url "$EXECUTOR_URL" --key-file "$EXECUTOR_KEY_FILE"
uv run --locked modelctl --state "$CONTROL_STATE" scheduler executor-probe --executor "$EXECUTOR_ID"
uv run --locked modelctl --state "$CONTROL_STATE" scheduler executors
uv run --locked modelctl --state "$CONTROL_STATE" scheduler controller
```

`executor-probe`經認證核對完整inventory；相同resource_id不能登錄為兩個worker。catalog保存
完整固定deployment契約，因此revision/image/context/dtype/各resource參數不同即不同ID。
目前真實Docker runtime僅支援單一可見GPU，無法取得唯一GPU UUID即拒絕inventory，不猜測身分。
CPU fixture明確標為cpu；其並行不能描述成實際多GPU驗收。registry目前offline維護，不熱加入worker。

`maintenance pause`原子停止新legacy/durable收件及dispatch，保留queue；`admissions`停止新收件，
讓既有queue繼續；`drain`停止新收件，完成既有queue與active lease後卸載。`open`恢復收件/派工。
`--executor ID`只維護該worker；worker drain解除未開始的規劃指派，交由其他相符worker接手，
已建立attempt不得遷移。管理與admission使用同SQLite交易；重啟維持mode。

```bash
uv run --locked modelctl --state "$CONTROL_STATE" scheduler maintenance drain
uv run --locked modelctl --state "$CONTROL_STATE" scheduler executors
# 確認所有worker unloaded、inflight=0、uncertain=0後，停止controller程序。
uv run --locked modelctl --state "$CONTROL_STATE" scheduler unload
# unload核對engine退出並釋放ownership；之後才停止executor/API程序。
uv run --locked modelctl --state "$CONTROL_STATE" scheduler maintenance open
```

未知工作無法靠drain變成完成；離線worker也不代表GPU已停止。先恢復原executor並對帳，或經
授權停止原engine、核對退出，保留unknown attempt，不重新提交取代它。正常drain保留active lease；controller停止後的明確operator `unload`可停止原engine，
但必須取得完整退出證據才釋放lease，unknown attempt仍保留且不重派。`resume-deployment REF --executor ID`僅在該worker
unloaded且controller已停止時解除其部署失敗標記。

## Client觀察與相容性

`GET /v1/executors`提供各worker的verified部署、availability、phase、maintenance、inflight與
uncertain；`GET /v1/scheduler`另含`all_ready`及`ready_executors`，不把單端ready當全體ready。
既有`/health/ready`、`/v1/models`與Chat/SSE固定對應primary（原單executor遷移保留原ID，
全新fleet取第一個登錄worker）；primary離線不會換模型或靜默fallback。其他worker仍可處理
符合完整deployment ID的durable jobs，`/health/live`與jobs/catalog不依賴primary模型ready。

jobs回應的`scheduling`含executor與reason：例如dependency_waiting、
not_in_verified_catalog、executor_offline、worker_maintenance、capacity_or_lifecycle或
execution_unconfirmed。queued指派可以解除；已有attempt的executor不可改變。結果仍以
`result_state=available`為可讀條件，pending/storage_failed只恢復儲存，不重新推論。

## 容量與保留

```bash
uv run --locked modelctl --state "$CONTROL_STATE" scheduler control-usage
uv run --locked modelctl --state "$CONTROL_STATE" scheduler executor-usage --executor "$EXECUTOR_ID"
uv run --locked modelctl --state "$CONTROL_STATE" scheduler executor-collect --executor "$EXECUTOR_ID" --retention-seconds 86400
uv run --locked modelctl --state "$CONTROL_STATE" scheduler collect
```

清理採明確操作，不在背景擅自刪除歷史raw。executor預設10,000 command slots／1GiB邏輯上限；
control預設10,000 command tombstones、10,000 grants／256MiB邏輯及預留上限。GC回收大payload、
receipt/output/frames，永久保留該authority的去重metadata；slot不返還，滿載拒絕，尚無authority
退休/輪替的自動化流程。接近metadata上限應停止新增工作、盤點未確認工作及備份，再規劃新
authority；不得手刪歷史去重紀錄來繞過上限。

ACK只確認控制端保存結果，release intent另確認沒有活躍輸出讀者；API singleton lock確認舊
reader已退出後，才可釋放它的legacy副本。durable receipt尚未ACK/release時，控制端job GC也
不刪其attempt/結果引用。中途失敗的ACK/GC可重試；SQLite可重用頁數、配置檔案與WAL大小分別
回報，邏輯回收不等於磁碟檔立刻縮小。lifecycle小型receipt保留在控制端支援remote GC後恢復。


## 本輪驗證範圍

候選runtime來源 `6f1046531c08fa56be7f14be4e5b0ea4067e9a53`。完整CPU回歸291 passed、2個
Windows平台skip；最後drain狀態修正另以受影響fleet store/controller 26項通過覆蓋。真HTTP
三程序（control＋兩executor）驗證18項Landlock拒讀、實際同時held執行、異質匹配、單端
死亡/重啟/未知不重派、過期fence、維護及ACK/GC。ASGI/SQLite故障注入另驗存滿、交易中斷、
receipt/ACK回覆遺失、活躍reader保護與metadata容量上限。另以真TCP截斷已成功ACK的回覆，
executor已GC後control仍以保存的receipt hash重ACK；推論只有一次，重啟保留同hash tombstone。
補測新舊HTTP process共2項通過，runtime來源不變。

單4090使用既有固定Qwen/Whisper image及完整模型revision，獨立候選state；原固定版與原始
資料保留。通過Qwen JSON/SSE/單圖/工具/合成影片解碼、legacy與durable共享2slots/429、
持久pause/admissions/drain、queued保留、cancel/detach保留lease至terminal、Whisper轉錄、
模型切換、排空退出、executor GC與cached lifecycle receipt恢復、正常三程序停止/重啟。
舊DB曾留stale ready而實際服務已停，先透過原協定確認舊engine退出，沒有刪lease/gate繞過。

本輪GPU窗口4 loads／35 generation attempts（含warmup預留），約447秒；未達5/48上限，
窗口已關閉、lifetime計數保留。觀察Qwen load-to-ready約116–119秒、Whisper約7.5秒；
混合工作Whisper等待約8.8秒／執行1.0秒，隨後Qwen等待約127.8秒／執行0.13秒。
這是本次固定配置下的觀測，不是跨硬體效能保證，也沒有另訂吞吐驗收門檻。

最終保留Qwen影片服務，ready=true且inflight/detached/uncertain全零；最終JSON/SSE真實
呼叫通過。沒有新增模型下載、變更runtime/image、開LAN或安裝開機自啟。正常重啟有驗證，
主機重開機/登出、真多主機/多GPU、產品辨識/轉錄品質與authority退休流程仍未驗證。
公開文件只保存契約與必要結果；主機路徑、credentials位置、容器metadata、raw失敗與成功
證據留在本機操作文件與忽略的evidence/raw目錄。初輪CPU整合介面不一致的失敗亦保留。
