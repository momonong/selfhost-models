# WSL managed 服務的程序監督

此文件是部署與操作方式；當次實際狀態與 deployment IDs 以交付證據為準。
API、controller、SQLite 必須在同機 native Linux filesystem。部署使用固定版本的
source/venv，不能讓服務直接跟著開發 working tree 更新。原 static state 與容器保留。

## 安裝與啟動

兩份 [systemd templates](../deploy/systemd/) 分別監督 API/controller。部署時將 `@USER@`、
`@RELEASE@`、`@STATE@` 替換為已核對的 Linux 使用者、固定 release 絕對路徑、獨立 state
絕對路徑。先以 `systemd-analyze verify` 檢查具體檔，再以 root 安裝至
`/etc/systemd/system/`。服務啟動直接使用已建立的 venv，不在啟動時下載依賴。

原 public key 安全複製到新 state 的 `api-key`，mode0600，以 `cmp -s` 核對，不印內容。
只有取得資源窗口、原 static engine 已退出且 gate 已安全交接後才能啟動 controller：

```bash
sudo systemctl daemon-reload
sudo systemctl enable selfhost-scheduler-api selfhost-scheduler-controller
sudo systemctl start selfhost-scheduler-api selfhost-scheduler-controller
systemctl is-active selfhost-scheduler-api selfhost-scheduler-controller
journalctl -u selfhost-scheduler-api -u selfhost-scheduler-controller -n 100 --no-pager
```

兩個 unit 獨立；controller 故障時 API 仍可查工作與 catalog。API 的 Docker socket 路徑
被遮蔽，controller 才操作 Docker。這不是隔離同使用者所有主機管理能力的安全邊界。
`Restart=on-failure` 最多每分鐘3次；只是程序監督，不是 GPU 自動恢復 ready。
故障／重啟後 controller 使用新 epoch，未知 attempt 不重送；engine 仍活時保留 unknown。
日誌不可包含 public/worker secret 或私人 payload。

WSL 的 systemd services 本身不保證 distro 持續存活，參閱
[Microsoft 文件](https://learn.microsoft.com/en-us/windows/wsl/systemd)。本輪可由 Windows
以 `Start-Process -WindowStyle Hidden` 啟動獨立 `wsl.exe -d Ubuntu --exec /bin/sleep infinity`
保活程序，保存 PID/開始時間並核對實際命令；不要結束其他任務的 wsl.exe。
正式交付必須在啟動工具已返回、client 已結束後，再從另一回合驗證 systemd active、
ready/models 與 Windows localhost連線。未測 Windows reboot，不能宣稱開機自啟。

## 正常停止與受控復原

先取得 clients／writers 已釋放的確認，再檢查 queue/leases；本版 API 沒有原子
maintenance seal。正常停止前，queued/dispatching/running/draining 工作、所有 leases
以及 pending/storage_failed 成果都必須為0，phase應穩定 ready/unloaded，API/controller
程序身份保持不變。未達條件就有界等待或停止操作，不先停API來等待既有 legacy drain。
停止 API 會終止其 legacy response producer；若競態中仍有請求，可能留下 unknown。
durable controller 不會接續該 legacy response，不能把它描述為正常 drain完成。

需做純唯讀 DB 核對時，對已存在的 `scheduler.sqlite3` 使用 SQLite URI `mode=ro`、
`query_only=ON` 與單一讀取交易；不要使用 `immutable=1`（會漏掉WAL），也不要為了
唯讀快照呼叫 `Store()`（建構子會初始化／遷移）。查 controller epoch/phase/heartbeat、
leases kind/state 聚合、jobs state/result_state 聚合，不輸出請求內容。

```bash
sudo systemctl stop selfhost-scheduler-api
# 確認API inactive，再核對上述idle條件；若出現unknown，停止並回報部分停機。
sudo systemctl stop selfhost-scheduler-controller
<release>/.venv/bin/modelctl --state <native-state> scheduler status
<release>/.venv/bin/modelctl --state <native-state> scheduler unload
```

`unload` 必須取得 controller lock，核對自身容器並證實整個 engine 退出後才釋放 lease、
pin及 gate。退出無法證明則停止，保留 unknown；禁止刪 DB／lease／volume繞過保護。
完成後核對 phase=unloaded、lease=0、model pins=0、engine_cleanup=0；歷史 unknown
outcome可以保留，但不能再有未知資源 ownership。
systemd 不設 `ExecStop=unload`，避免非預期服務停止直接操作 GPU。
完成卸載後才能重新啟動 units、提交指定 deployment 的 durable job並重新確認 ready。
若部署曾因生命週期失敗 blocked，僅在 engine 退出已確認後由管理者明確 resume。

單獨重啟 controller，即使先前 ready 且沒有工作，仍可能轉為 unknown；本版不採用
自動接管活 engine。操作員使用上述受控 unload 流程，不把 active或heartbeat當ready。

## Windows client 與模型切換

正式 origin 保持 `http://127.0.0.1:18080`，key 從已確認專案根目錄下的
`.state/api-key` 讀取，client 以 `--api-key-file` 指定實際位置；不開 LAN 或改 WSL 全域網路設定。
所有 `modelctl scheduler` client 指令傳 `--url http://127.0.0.1:18080`，deployment ID
從該服務 catalog 取用。具體可重現合成 submit/get/result/cancel 範例隨當次部署證據交付。
Whisper ready 時舊 Chat API 不接受 Qwen 工作；以 Qwen durable job 明確切回，無自動 fallback。
