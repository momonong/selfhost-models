# 原生 Linux managed 驗收：兩輪結果與持續服務

**最終狀態：第二輪完成 managed Qwen／Whisper 工程驗收，正式 Qwen 影片服務留在
loopback 18080，API/controller 受 user transient systemd 監督。** 原部署及資料保留。
沒有開機自啟、LAN／公網、多機或產品品質驗收。以下保留第一輪失敗，再記第二輪結果。

## 第一輪：preflight 受阻，原 static 回復

本輪沿用單一 checkout，由既有 task 直接回報本機 main；起點
`aaa296ff13737503380649a8e6a8b2d2d1a90392`，工作分支 `fix/linux-managed-acceptance`。
程式修正 `369ee39f292c78cddce0c10a4a5e0c6a377f5268`；未合併、推送或發布 image。
這是新 Linux 桌機證據，不延用筆電 GPU 窗口或歷史配額。

### 第一輪已完成與未完成

| 項目 | 結果與範圍 |
|---|---|
| 主機 CPU 契約 | Ubuntu／Python 3.12.3／uv 0.11.21，192 passed、2 Windows-only skipped，16.07 秒 |
| ready 修正 | 同已驗證 deployment／epoch 的 HTTP identity probe 不再暫時清除 ready；probe 完成後重查權威健康與身份。生命週期、epoch、identity 失敗仍拒絕 |
| 競態回歸 | 事件同步覆蓋 probe 中 SSE admission、斷線後 lease 保留，以及首次探測、draining、unknown、epoch/deployment 變更、heartbeat 過期、identity 失敗 |
| 固定候選 images | [發布清單](../2026-09-23-scheduler-publication/README.md)三個 digest 已拉取，核對 config／OCI source 與實際 18／3／27 檔；正規化 CRLF 後吻合 `c7f35e1` Git bytes |
| 資產 | 既有 Qwen 固定 revision 完整 hash 登錄；Whisper-small 固定 `973afd24965f72e36ca33b3055d56a652f456b4d` 的必要 11 檔下載完成，官方 metadata、size、完整 SHA256 均吻合 |
| Linux 程序監督 | 固定 source／獨立 locked venv；user transient CPU API 可跨工具回合存活，Docker socket 遮蔽。未驗證登出、重開機；無開機自啟 |
| managed GPU | **resource-blocked**：controller preflight 偵測到 GPU ownership 衝突；epoch 0、0 loads／0 generations／0 attempts，沒有 managed engine 或 model pin |
| durable HTTP | catalog、3 次 submit、持久 job ID、get 與 queued cancel 已執行；3 jobs 均在 dispatch 前取消，沒有推論結果，不算 submit/get/result 全流程通過 |
| 完整 D | ①③②、Whisper 真 GPU、urgent／capacity、running cancel／disconnect、controller/API restart／fencing／engine exit 回收及正式 managed 常駐均未覆蓋 |
| static 回復 | 原容器、原 images、模型/revision、8192／capacity2／video／GPU0.60 不變；ready/models/零 lease、一次真實 JSON 文字呼叫、client 與工具退出後存活均通過 |

修正只影響 host managed API；本輪未用舊 API 候選執行 managed Gateway。
vLLM／Whisper 的有效 worker 程式保持發布版本；未換 runtime、PyTorch 或模型。
192 項 CPU 證據可覆蓋結果保存失敗／恢復等契約，但不替代任何 GPU 驗收。
歷史 SSE non-200 未保存 status/body，仍不能斷言其根因就是已重現的 ready 競態。

### 第一輪阻礙與處理

第一輪因 GPU ownership 衝突而被 preflight 拒絕。`check_other_gpu_owners` 在
controller start 與每次 prepare 都執行；檢查 device mapping，不能只看當前 compute PID。
沒有放寬 preflight 或繞過 ownership 保護，先回復原 static 服務。

此衝突應在停止 static 前發現；本輪前置盤點漏掉完整 device mapping，
故發生可避免的 static 中斷。回復流程保留
全部 images、容器、模型、state、journal 與歷史 evidence。
controller 受 systemd 三次啟動上限約束，每次皆在 reserve/load 前被拒。
回復前驗收腳本曾將所有 pins 都要求為零；實際留下的是已取消 job 的輸入 artifact
保留引用，不是 model pin。修正檢查只區分 model pin 與 artifact retention，未刪任何 pin。

### 第一輪窗口與配額

首次 static stop 起單一 monotonic deadline：90 分鐘，T+60 停新驗收 dispatch，
後 30 分鐘僅交付／回復。所有外部命令 timeout 受剩餘期限裁切，命令逾時記 unknown；
獨立 deadline guard 與有界資源監測不把 controller 停止當作 engine 退出。
實際從 stop 到完成 static 回復驗證約 227 秒；資源監測使用實際 timestamp，
不宣稱 blocking command 能嚴格每秒採樣。

| 範圍 | 核准上限 load／generation | 實際保守記帳 |
|---|---:|---:|
| acceptance | 5／47 | 0／0 |
| formal service window | 1／9 | 0／0；按契約關閉窗口，保留歷史 |
| static rollback | 1／7 | 1／7（6 次暖機上界＋1 次真文字 smoke） |
| 合計 | 7／63 | **1／7** |

失敗／取消不退款、未重設 DB 或刪 gate。原 static 以新版 CLI restart 時取得自己的
ownership gate；gate 保留，未用手動刪除繞過資源檢查。後續維護仍須核對其他 device
容器；不能假設 static stop 後 gate 一定能釋放。

### 第一輪保留資料

主機識別、路徑、容器資訊、完整狀態與輸出，以及驗收 driver、固定部署 artifact、
DB 與 key 均留部署端，不納入公開紀錄。

最終只有原 static 在 loopback 原入口提供服務；managed API/controller 已停止。
正式 managed 操作例子與日常 submit/query/result/cancel 交付尚未完成。
接續前須重新確認資源前置條件、owner、期限及配額；不能使用本輪關閉的窗口繼續測試。
Whisper 下載成功與服務推論成功分開，語音辨識、影片與產品品質都未因此通過。

## 第二輪：managed GPU 工程驗收通過

第二輪在新的授權窗口內重新完成 GPU ownership 與資源前置核對後執行。
沿用原 acceptance/service DB，保留第一輪 canceled jobs、artifact pins 與窗口，
建立新的具名窗口，沒有用新 state 或刪 journal 重置額度。

| 案例 | 結果與證據邊界 |
|---|---|
| durable submit/get/result | Qwen 與 Whisper 真 GPU 成功，持久 job/attempt ID、description 與正常 result 可查 |
| 固定①③② | 在 Qwen ready、reuse_count<3、未達 aging 門檻時，dispatch 與完成順序均為①③②，附 deployment/load/events |
| urgent FIFO／安全邊界 | normal 先入列，urgent①②依序先 dispatch；不搶占仍持有 lease 的 Whisper |
| running cancel | Whisper cancel_requested 後仍觀察到 lease；terminal 之後才解除，結果保留 canceled 語義 |
| Qwen 能力 | durable 文字、PNG、合成工具呼叫與回送、2秒合成 H.264 影片含解碼 metadata、完整 legacy SSE |
| disconnect／capacity | 真 SSE 關閉 client 後觀察 detached lease；legacy+durable 共同占兩個 lease，額外 legacy 回429，terminal 後歸零 |
| API/controller 重啟 | 活請求期間換代，controller epoch 1→2，兩個 request lease 均 unknown、model pin 保留；API PID 更新 |
| whole-engine 退出 | finally unpause；受控 stop 後 Running=false、Paused=false、Pid=0、OOMKilled=false，ExitCode137；不是優雅完成，也不把 unknown 結果當成功 |
| fencing／回收／恢復 | 退出確認後 epoch3、phase unloaded、lease/model pin/cleanup均0；epoch4重新載入Whisper真GPU成功，原 unknown job仍僅一個attempt，沒有重派 |
| 結果保存失敗／恢復 | 重用同 source 的192項CPU契約；沒有額外GPU保存故障案例 |
| 正式服務 | 獨立正式 state 完成Qwen暖機、durable result、一般JSON及完整SSE；臨時1/9窗口按契約關閉，lifetime累計保留 |
| 持續運行 | client與啟動工具結束後，再一回合確認API/controller active、ready/models與lease全零；API的Docker socket實際映射為mode000 |

正式 API/controller 使用固定 `369ee39` source artifact／独立 locked venv，
vLLM worker 使用已核對的發布 digest；正式資產仍為固定 Qwen revision
`851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`。Whisper 由 durable job 指定其 catalog
deployment 切換；不並載、不 fallback。測試結果不代表語音轉錄或影片內容判斷品質。

### 保留的失敗與驗收工具修正

1. 最初三個工作在冷啟動前同時入列，等待超過60秒 aging，因此依法完成①②③。
   該次對①③②的 assertion 失敗原樣保留；修正的是案例前提，沒有改 aging/reuse政策。
2. 初次 SSE detach 在 client close 後立即讀 DB，早於 server 處理 disconnect；
   當時仍為active lease。保留此未捕捉邊界結果，再使用一次預留生成、5ms間隔且
   最長1秒的有界觀察捕捉 detached，未退款或更動服務。
3. 故障 engine 已退出後，驗收 helper 再次 stop 已被 systemd 回收的 transient
   controller unit 得到 not-loaded。核對程序已退出後完成原定 unload acknowledgement，
   沒有重跑故障或新增推論；helper 已區分已回收 unit 與仍在運行程序。

### 第二輪配額、期限與配置調整

新窗口沿用90分鐘、T+60禁新驗收，沒有因補測
重開窗口。main 明確核准總load7→8，generation仍63、截止時間不變；第一輪用量獨立。
補測需要的 lifetime／active window load5→6，只在原定故障已確認整engine退出、
無lease/model pin/cleanup/queued工作、API/controller/writers停止與雙排他鎖成立後執行。
SQLite backup讀回核對、BEGIN IMMEDIATE、前後logical hashes與artifact/receipt完整hash
證明只有兩個目標load欄位改變並追加核准事件；已用5/42、所有jobs/attempts/results與
既有events保持不變。重開Store與config檔讀回一致，沒有熱改活engine設定。

| 第二輪範圍 | 最終核准上限 load／generation | 實際保守記帳 |
|---|---:|---:|
| acceptance | 6／47 | 6／44 |
| formal service | 1／9 | 1／9 |
| static rollback預留 | 1／7 | 0／0，未動用 |
| 第二輪合計 | 8／63 | **7／53** |

兩輪合計為8 loads／60 generations保守記帳；沒有將第一輪1／7抹除或從第二輪退款。
第二輪約1040秒完成，資源樣本未觸及停止門檻；原始timestamp保留，不把採樣視為GPU租約。

### 最終保留與操作

正式 API/controller 為 user transient units；不使用 Windows keepalive，不安裝或
enable開機自啟。已驗證工具/client退出後存活，**未測登出與重開機**。重開機後需
由操作員核對containers／gate／DB並重新啟動，不能把systemd active當模型ready。
原 static 容器保持停止，資料與配置保留。任何後續啟動仍須重新核對 GPU ownership。

第二輪完整原始證據與本機操作紀錄留部署端。固定source artifact、兩份state、模型、
候選images、原容器、工作分支均保留，沒有新增Git worktree。
跨平台HTTP/CLI流程仍以 [scheduler契約](../../docs/scheduler.md) 為準。
