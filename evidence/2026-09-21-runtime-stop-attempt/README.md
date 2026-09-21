# 正常停止 Docker Desktop 的實際 STOP

使用者已直接批准精確 runtime 修復。原窗口到期後，main 確認新一輪90分鐘窗口，
並保留原 ledger、UNKNOWN 與累計12 loads／64 generations；沒有重設使用量。

| 邊界 | 台北時間（2026-09-21） |
|---|---|
| 新窗口起點 | 07:57:16.970 |
| T+60，停止新增驗收案例 | 08:57:16.970 |
| 階段 hard deadline | 09:27:16.970 |
| 本次480秒恢復鏈截止 | 08:05:15.244 |

本次 `docker desktop stop --timeout 55` 在07:57:16.981送出。
client 於58.016秒超時，client cleanup完成；stdout只有
`Failed to stop Docker Desktop`，stderr空。外部停止結果仍為UNKNOWN。
唯一後續退出確認顯示同六個 Desktop/backend owner仍在，因此工作於07:58:15.598
以 `owner process remains: no rename` 停止，整個流程60.343秒。

**實際派送：stop 1、rename 0、start 0。** 沒有第二stop、force、process-tree kill、
WSL shutdown、重開機、設定修改或刪除。兩個runtime目錄留在原處，沒有quarantine。
修復的1／6仍為未派送的load/generation預留，不能因送出stop就稱已load。
較早start的UNKNOWN保守1／6仍保留。

退出確認時Ubuntu與docker-desktop均Stopped；原static lease journal仍為0，
SHA-256維持`de0dba4842b264e04b1d184a1e1d6de5e6be1883927ff9fadaec42f947a8bbd6`。
停止前GPU使用2423MiB，Windows可用記憶體約36.27GiB。
這些是host快照，不能代替daemon ready、目前全部容器身份或managed部署證據。

08:02:18再次只讀核對六個PID／creation time／完整exe路徑／owner：都是同一Windows
使用者擁有的Docker官方安裝目錄中的兩個backend、四個Desktop process，身份與之前一致。
精確PID、個人帳號與路徑留在私有receipt；沒有對任何process執行終止。

## 待使用者正常退出後的續作

官方[Docker menu說明](https://docs.docker.com/desktop/use-desktop/)列出系統列的
Quit Docker Desktop。這個UI方法是否經同一個失效API、能否成功退出，尚未證實。
本task目前的CUA原生電腦API停用，沒有可用的Windows原生UI控制工具；不以自訂
Win32關閉訊息或另一條CLI代替未批准的動作。main統一請使用者手動Quit並回報。

已準備私有入口：

```text
.state/scheduler-deployment-20260921/continue_after_manual_quit.py --after-manual-quit
```

**只做AST檢查，沒有執行。** 入口沿用原恢復鏈absolute deadline，至少預留260秒
（rename20＋start180＋五個只讀命令各10＋收尾10），剩餘不足或到期即在
建立新receipt目錄前停止；不自動延長480秒。使用者回報後仍須先只讀核對時間与owner，
由main確認當時有效的有界續作，不能直接照抄上面的入口繞過到期條件。

續作不含stop。它核對全owner退出、whole engine退出、原journal、普通父目錄、固定
AF_UNIX清單與dest不存在後，才可能保留式rename兩個目錄，再用尚未派送的一次normal
start。部分rename、未知start或資源異常立即STOP，保留新舊資料和receipt。
daemon／原image身份／lease／Windows RAM／GPU驗證後仍要核對native WSL RAM≥8GiB；
未完成全部gate不得開始GPU或把snapshot當RESOURCE GO。

此處沒有完成candidate build、managed服務、GPU驗收或部署。原容器、images、模型、
資料、settings與VHDX保留，未merge／push。證據hash及未執行的續作來源見
[validation.json](validation.json)；先前runner的5項Windows測試不因本次操作再次重跑。
