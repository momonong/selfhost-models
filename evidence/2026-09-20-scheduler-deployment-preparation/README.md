# Managed 部署離線準備：2026-09-20

**離線準備已完成；Docker 恢復、GPU 驗收與正式部署尚未執行。**
此輪從 `e55ac0f` 延續同一 `feat/single-host-scheduler` working tree，沒有額外 worktree、
merge、push、發布或跨專案修改。完整基礎 CPU 證據仍保留於
[前一份證據](../2026-09-20-scheduler-cpu/README.md)，不改寫其 image/source hash。

## 本輪變更與核對

- 臨時 budget window 以同一 SQLite transaction 限制 load/warmup、durable 與 legacy
  admission。lifetime 不重設，關閉窗口後歷史計數固定；未知資源仍阻止關閉，可信 engine
  exit 後可保留 unknown outcome並關閉窗口，工作不重派。
- Windows 完整回歸 **174 passed / 15.81s**；native WSL scheduler/process/boundary
  **57 passed / 6.87s**。5個真管理 CLI 指令（init/open/status/close/status）通過。
- 兩份 systemd template 代入本task既有native CPU scratch的具體路徑，
  `systemd-analyze verify` exit0。未安裝或啟動 unit，不能據此宣稱服務存活。
- [validation.json](validation.json) 保存命令、範圍與結果；
  [source-sha256.json](source-sha256.json) 保存實際檔案 bytes SHA256。
  Store/CLI/tests及兩份unit與Linux測試快照逐檔比對一致。

原 `/tmp` CPU scratch 已不存在；本輪在 native Linux新建私有CPU scratch並從未改動的
uv.lock重建venv。離線cache缺少av wheel，隨後僅同步鎖定Python依賴；沒有模型或image下載。
私有log/XML/CLI輸出保留於 ignored `.state/scheduler-deployment-20260920`，不提交key、
SQLite、完整主機盤點或個人payload。本輪CPU scratch保留供追溯，無背景fixture程序。

## 待接續範圍與停止條件

[D runbook](../../docs/scheduler-gpu-acceptance.md) 已更新為90分鐘、12 load／64 generation，
按initial1/6、acceptance8/44、formal2/8、rollback1/6分配。90分鐘與fresh owner截止取較早；
T+60停止新case。私有window ledger目前 `not_started_no_resource_go`，未建立執行起點。
正式state獨立於驗收state；關閉臨時window後保留長期計數，不把耗盡驗收DB當正式服務。

本輪最後一次有界唯讀preflight（12:49:36 UTC）：Docker回「unable to start」，18080 ready
5秒timeout。之後未反覆probe。協調端轉達restart GO時被自動批准審查拒絕，main正在取得
具體直接確認；本task未重送被拒操作或改路徑操作共享資源。

可審閱的唯一恢復命令是 `docker desktop restart --timeout 180`，但目前**不可執行**。
取得可核對直接確認與fresh資源窗口後，依
[恢復runbook](../../docs/scheduler-incident-recovery.md)維持單一480秒monotonic deadline，
外層子程序同樣限180秒；nonzero/timeout立即STOP，不接續probe、重試或WSL/host重啟。
初始static自動reload／warmup及不確定結果都占全局budget，不能漏算。

尚未完成：Qwen候選build、此次完整來源的Whisper候選refresh、GPU切換/取消/復原case、
systemd實際啟停與跨工具存活、Hidden WSL keepalive、正式18080/public key交接，以及
使用實際catalog IDs的Windows合成使用範例。舊Whisper `6efb33…` 是舊來源快照，不能
冒充此次完整來源一致；新版Store/CLI雖屬host功能，image仍copy整包，須更新或明確歸屬。

[服務操作文件](../../docs/scheduler-service.md)含安裝、啟停、unknown處置與限制。
fresh clients釋放是正常stop前提，本版沒有原子maintenance seal；active不是ready。
未進行GPU推論、ASR/影片品質評估、Windows reboot或生產使用驗收。
