# Docker 啟動 STOP 與 Windows 控制程式修正

此紀錄截至 **2026-09-20 17:40:14 UTC（台北 9/21 01:40:14）**。
當時 daemon 未取得可用證據，managed 部署與 GPU 驗收維持 STOP。
此處僅交付有界控制程式及診斷證據；不是部署完成或模型品質驗收。

## 已確認的事件

| UTC | 事件與證據界線 |
|---|---|
| 9/20 14:52:47–14:53:34 | 前一窗口的唯一 Desktop restart 回傳 exit 0；之後已核對原容器身份與退出狀態。不能改稱這次 restart 失敗。 |
| 17:13–17:14 | 續作時 desktop-linux named pipe 不存在、Desktop/backend 未執行、兩個 WSL distro 停止。 |
| 17:18:45 | 已授權的一次正常 Desktop start 送出。舊 Windows wrapper 超時後仍等待輸出；CLI 結果 UNKNOWN，沒有重送。 |
| 17:18:59.7845055 | 新 backend log 記錄 Inference manager 初始化失敗：無法移除 `Docker/run/dockerInference`，並同時記錄 local engines stopped / shutdown complete。這是啟動錯誤證據，不是現在容器或 GPU 全部停止的證明。 |
| 17:23 | 僅在核對 PID／命令列後結束本任務的 wrapper；未終止 Desktop、backend、WSL 或其他服務。 |
| 17:31:21–17:31:30 | 修正後的只讀診斷共 9.031 秒。context 為 desktop-linux/local named pipe；單次 version client 在 8.016 秒超時並完成該 client 清理，外部結果仍 UNKNOWN。後續 ps/inspect/resource 檢查未執行。 |
| 17:38:59、17:40:13 | 只讀 runtime metadata、父目錄 ACL、指定非機密設定鍵；未再 probe daemon 或操作 lifecycle。 |

原 start 的 UNKNOWN receipt 與舊窗口 ledger 保留不覆寫。累計上限仍是
12 load / 64 generation；start 後實際 GPU 使用未知，initial recovery 的 1/6
保守計入且不退回。歷史 start 前觀察到的 0/0 不代表 start 後仍是 0/0。

## Windows 有界執行器

[`scripts/bounded_command.py`](../../scripts/bounded_command.py) 將 stdout/stderr
寫入新檔案，使用 `Popen.wait`。流程 absolute monotonic deadline 與單步上限取較早者，
同一額度內預留 client kill/wait；只操作該次 Popen handle，不終止 process tree。
過期 deadline 不建立 process。timeout 一律標記 external outcome unknown，
即使 client 清理成功也不能推定外部動作失敗或安全重送。

Windows 的 `subprocess.run(capture_output=True, timeout=...)` 在 timeout 後可能進入
沒有 timeout 的 `communicate()`；子孫持有 PIPE 時可能持續等待。這條程式路徑與
獨立 fixture 已確認，原 Desktop start 中究竟哪個程序持有 handle 仍未知。
普通輸出檔可能繼續被存活子孫寫入，讀取/hash 都是指定時刻的快照。
OS 若讓 CreateProcess/kill 本身停滯，不能承諾實時硬界限。

Windows 針對性驗證：**5 passed in 3.86s**，涵蓋非零 exit、已過期 deadline、
總額度包含清理，以及真實 Windows 子孫繼承 stdout 的 parent-exit／timeout 兩種案例。
fixture 子程序仍存活的證據是 Windows process handle wait；最後由 fixture 自己
讀 stop marker 退出，無 Docker、GPU、網路或服務操作。
這次沒有重跑先前的 scheduler 契約測試；先前 174 Windows／57 Linux 結果見
[部署準備證據](../2026-09-20-scheduler-deployment-preparation/README.md)。

私有 start/build Windows 呼叫程式已改用同一 runner、單一 deadline 及有限輸出讀取；
inspect 在捕獲前只選需要欄位，不保存 Env/secret。這些 caller 僅 AST 檢查，
candidate build 沒有執行，不能把語法通過當成 Docker 執行驗證。

## 最小只讀 metadata 結果

- `Docker/run`：普通目錄，非 reparse。直層恰兩項 `dockerInference` 與
  `userAnalyticsOtlpHttp.sock`，皆 size 0、AF_UNIX reparse tag `0x80000023`。
- 同層 `docker-secrets-engine`：普通目錄，直層只有 `engine.sock`，同為 size 0 AF_UNIX。
  沒有讀 socket 或 secret 內容，也沒有遞迴／追蹤 reparse target。
- 三個普通父目錄 ACL 可讀，觀察到 SYSTEM、Administrators、目錄 owner 的 FullAccess，
  無 deny；這不證明特殊 socket 正常或可移除。原 PowerShell Get-Acl 模組載入失敗後，
  改以只讀 Win32 security metadata 取得 ACL，未改權限。
- `settings-store.json` 未出現 `enableInference`、`enableInferenceTCP`、
  `enableInferenceGPUVariant`；缺席代表這份檔案未明列，不等於功能停用。
- 當時 Desktop 四個／backend 兩個 process 仍存在；不能把 UI 存活當作 daemon ready，
  也不能因此推定 runtime socket 無 owner。

初次 metadata client 在 cp950 JSON 解碼失敗，保留失敗 receipt；同一分鐘內修正解碼
完成讀取。另補讀正確 sibling 路徑，未將錯誤巢狀路徑的不存在當作 sibling 不存在。

## 待協調的修復候選

metadata 支持將明確失敗的 `Docker/run` 保留式 rename 作為候選，但 AF_UNIX 標記本身
不是 corruption 證據。前提是正常停止 Desktop 並確認相關 owner 消失，再使用不存在的
同父目錄 quarantine 名稱；不能覆寫、刪除或修改設定。`docker-secrets-engine` 尚無本輪
失敗 log，列為額外候選，沒有自動納入。此 snapshot 時尚未執行任何 rename 或再次 lifecycle。

具體路徑、owner PID、原始 log 與 ACL/SID 留在 `.state/scheduler-deployment-20260921/`
私有 receipts；可提交證據只保留去識別摘要與原始 receipt SHA-256。
來源與檢查結果見 [validation.json](validation.json)、[source-sha256.json](source-sha256.json)。

## 補記：定點修復於執行前遭自動批准審查拒絕

17:46 UTC 前，協調路徑已确认把上述兩個普通 runtime 目錄納入同一次保留式修復，
但實際 `exec_command` 在 **CreateProcess 前被自動批准審查拒絕**。
本次修復的 stop／rename／start 都是 **0 次**，repair script 未啟動；沒有換工具、
host、拆命令或以其他方式重送。這不改變較早正常 start 的 UNKNOWN。

審查理由是這組動作會停止共用 Docker Desktop、移走 runtime 目錄再啟動，可能中斷
服務，而可信使用者訊息未明確涵蓋這組具體副作用。原始拒絕已交來源 orchestrate，
由 main 處理直接使用者批准；此證據不是該批准，也不授權重新執行。

已離線同步配置：初始＋修復2 loads／12 generations、驗收7／38、正式2／8、
rollback1／6，合計仍12／64。原 UNKNOWN 保守占用1／6；額外修復1／6只是預留，
不是這次已耗用。時間窗口仍至台北02:48:45，新驗收截止02:18:45，沒有延長。
新配置與準備腳本語法已核對，沒有建立 native deployment/state 或啟動 GPU。
對應可提交摘要見 [repair-review-block.json](repair-review-block.json)。
