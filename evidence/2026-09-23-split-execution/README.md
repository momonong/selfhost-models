# 可分離控制端／執行端 v1：桌機工程驗收

完成本階段工程驗收，正式 Qwen 影片服務保留在 `127.0.0.1:18080`。
固定執行來源為 `19c4b89d4b576b92b976dd177e8722e08f69c906`；沿用既有 checkout，
工作分支 `feat/split-execution-plane`。沒有合併、推送、image 發布或開機自啟。
本紀錄是新桌機證據，不引用筆電成功作為桌機驗收。

## 改變與不變的契約

控制端持有公開 API、排程、SQLite jobs/attempts/leases/results；獨立 executor 持有
Docker/GPU、權重實體路徑及命令 journal。兩端經有認證且版本化的 HTTP 協定傳輸。
既有 deployment ID、模型 revision、worker image digest、公開 Chat/SSE/jobs 契約不變。
API/controller 沒有 Docker socket 或權重存取；executor 不讀控制端 DB/artifacts。

同一次 admission 交易保存 grant；durable 與 legacy 共用容量/生成預算。持久命令重送
不再執行，舊 fence 拒絕新操作，重啟中的不確定執行保留 unknown。結果先保存控制端
receipt 才 ACK；保存失敗只重試保存。SSE observer 結束不取消 producer，terminal receipt
保存後才送 DONE。模型只在 executor 驗證完整 manifest、解析既有唯讀路徑，不下載權重。

## 驗證結果

| 範圍 | 已驗證結果 |
|---|---|
| Locked CPU | Python 3.12，232 passed、2 Windows-only skipped，22.49 秒；依賴/lockfile未變 |
| 協定故障 | duplicate/conflict、回覆遺失後同命令取回、fencing、重啟 unknown、receipt/ACK、儲存失敗恢復；未重派生成 |
| 控制恢復 | 原 attempt owner 不改寫，以當前 fence 驗原 immutable grant匯入；legacy/durable共用 admission；migration副本原資料表內容hash保持一致 |
| 真程序隔離 | 兩個實際子程序、loopback HTTP、Landlock；6項跨端DB/artifact/權重/目錄存取均PermissionError，非mock存取、非skip |
| 隔離後執行 | CPU fixture經HTTP完成Qwen/Whisper生命週期、JSON、WAV bytes/hash、SSE/斷線、4個唯一生成與terminal ACK；無共享模型或控制檔案 |
| Qwen真GPU | 文字、單圖、工具呼叫/回送、合成MP4實際解碼、一般SSE及影片SSE metadata |
| 資源與交付邊界 | SSE斷線後detached lease、legacy+durable占兩格、第三個legacy429；影片deadline504、無效影片受控拒絕後ready恢復 |
| Whisper真GPU | 完整卸載Qwen後載入，canonical WAV只透過bytes/hash傳送；running cancel仍保留lease直到worker terminal，不是cancel-before-GPU案例 |
| 排程 | normal先入列，urgent①②依FIFO先dispatch，安全等待Whisper terminal後才切回Qwen |
| 重啟故障 | 在精確自有engine暫停且有兩個活請求時重啟executor/controller/API；保留unknown leases/model pin，未自動重派 |
| 退出與恢復 | finally unpause，再確定整engine退出（exit137、非OOM、不是優雅完成），才回收leases/model pin/容器；重新載入Whisper真GPU成功，原unknown job仍只有一個attempt |
| 正式交付 | 獨立正式control/executor state；Qwen影片ready/models、durable result、一般JSON與完整SSE，client/tool退出後再次核對active/ready、所有lease0、生成receipt全ACK |
| 實際程序隔離 | 正式API/controller的Docker socket、權重與executor state，以及executor的control state/公開key路徑，實際namespace映射mode000 |

GPU worker images沿用[已驗證發布清單](../2026-09-23-scheduler-publication/README.md)，
沒有重新包裝worker或變更runtime。API/controller/executor以固定source artifact及獨立locked
venv執行，不隨工作目錄修改變動。沒有替host安裝PyTorch或修改driver/Docker設定。

## 保存的失敗及修正

- CPU回歸發現等待生命週期鎖的舊命令可越過新fence，以及已取得結果後暫時儲存失敗會
  丟棄唯一輸出；均修正並加入回歸，固定候選包含修正。ACK SQLite error及中斷retire的
  恢復也納入處理；unknown生成不因此重派。
- 首次GPU監測在原服務正常卸載期間發生命令失敗，當次未保存完整命令細節；容器枚舉
  與退出競態是推論。實際核對GPU空、原engine已退出、候選仍0/0後，修正監測處理及錯誤
  保存。保留原失敗，沿用同一窗口、deadline與配額，未刪lease/journal或重置計數。
- 影片推論首次腳本讀到`succeeded`但結果尚在發布，立刻取result得到409。後來同一結果
  為available且有解碼metadata；修正腳本等`result_state=available`，直接取原結果，未重跑。
- 故障注入的unknown工作、exit137及全部raw輸出保留，不把未知結果算成功。

## 有界窗口與最終狀態

新窗口上限90分鐘、T+60停止新驗收，總8 loads/70 generations；實際約782秒完成。
暖機依既有保守上界計入，取消/失敗不退款。沒有使用歷史已關閉窗口。

| 範圍 | 上限 load/generation | 實際 |
|---|---:|---:|
| 驗收 | 6/52 | 4/35 |
| 正式候選交付 | 1/9 | 1/9 |
| 舊版回復預留 | 1/9 | 0/0 |
| 合計 | 8/70 | **5/44** |

驗收與正式臨時窗口均已關閉，lifetime計數保留。驗收state保留一個unknown durable job；
正式state沒有unknown工作或lease。正式Qwen影片採context8192、capacity2、GPU fraction0.60，
兩個backend不並載、不fallback。原managed固定部署/state及static容器保留且停止。
沒有新worktree或repo副本。

主機識別、路徑、容器與程序資訊、keys、完整原始輸出及本機操作紀錄均留部署端，
不納入本摘要。通用契約在
[階段規格](../../docs/split-execution-plane.md)及[client接入](../../docs/client-integration.md)。

## 邊界

只完成原生Linux單主機工程驗收；未驗證分機、LAN/公網、其他OS、登出或重開機。
沒有開機自啟。executor journal仍採有界保留，滿載拒絕，尚未實作長期GC；需依文件監控容量。
模型下載沿用先前已驗證權重，本輪沒有新HF下載結果。合成影片/靜音WAV成功不代表桌球判斷、
快速攻防或語音轉錄品質可靠。回復舊版文件與資產保留，但本輪未實際執行回復演練。
