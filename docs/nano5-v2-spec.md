# Nano5 v2：專案與 job 導向規格

已接受流程：一次登錄完整程式專案 → 選專案建立 job → 查看工作與結果。job 是交給 Slurm 分配資源、執行程式的工作；本輪僅本機準備，沒有真實提交或排隊。

起點：`feat/nano5-batch-ui`、HEAD `e1f3c2e6b3a2b6744142cb037b3e63a35d2c4f39`，v1 dirty 成果沿用。main 人類訊息 `01a0fa58-8022-7810-a67e-ddf936777186`「好 那我覺得五們可以時做了」已由原始對話核對；方案來源 `01a0fa53-04eb-7673-8aed-77c7f9eb07e4`。main `01a0f890-f803-7e93-8f28-47a591b19b66` → orchestrate `01a0f8a9-5f8c-70f3-9c17-9cb634a296a0` → task `01a0f8ab-5eda-77c1-a425-38c678b3cc06`，沿用 UI subagent 與 reviewer `01a0f8b5-16c6-7952-b828-45aea36fd00a`。僅交換本階段必要非秘密規格/程式/證據，例行成果留原 task。

## 行為與資料契約

- 主導航「工作」「專案」，預設工作頁；新手空狀態建立專案。離線示範另有次要入口，舊版資料另列，不能假稱已排隊。
- 專案具名稱、程式快照/階層、相對工作目錄、入口/預設參數、環境及資料位置預設。瀏覽器明確選資料夾一次匯入；排除 .git/.venv/cache/憑證等逐項顯示，保留 16 MiB/100 檔小型程式限制，不搬大型資產。
- 環境是具名稱的既有遠端 SIF metadata；可先以「待核對環境」完成準備。資料位置為具名稱的 dataset/model 遠端絕對路徑 metadata，未連線核對；本機不讀任意路徑。
- Job 草稿在建立時固定專案版本/程式 bytes、環境及位置快照，保存參數與資源。準備後工作不可變；修改專案不改既有草稿/工作。複製工作建立新草稿及新意圖，保留原設定與程式版本。
- 資源範本：短程測試 dev/H100/20 分、一般訓練 normal/H100/60 分、較大 GPU 記憶體 normal2/H200/60 分；單節點/單 process，Slurm 選節點。不猜 VRAM、權限、排隊或帳務。
- 生成腳本的程式與遠端資料唯讀 bind，明確 BATCH_INPUT_DIR/BATCH_OUTPUT_DIR/BATCH_DATASET_DIR/BATCH_MODEL_DIR/BATCH_ASSET_n 契約；結果唯一目錄、不覆寫。不下載/安裝或自提交。
- 正式 submit 仍 403，無 env/CLI/UI 開關。unknown 不重送、cancel 不證明停止、匯入成果不確認 terminal、available 成果 bytes/來源保留。
- SQLite 僅 additive schema；舊紀錄無杜撰專案來源。先備份既有 18771 state，副本試遷移，核對每表行數/內容雜湊、integrity/foreign_keys、舊下載，再切換本人預覽。保留備份/rollback。

## 驗收及停止邊界

真瀏覽器完成建專案→新 job→重用第二 job→改專案舊 job 不變→結果/複製；390px主要流程。完整既有 CPU 回歸、migration/快照/安全位置/狀態契約測試，最終 wheel/static/runtime bytes 一致。原 reviewer 做一般靜態或正常操作複核，歷史被攔的攻擊性動態重現不重做。

僅本機 CPU、必要依賴及既有 loopback 18771 本人預覽重啟；不 commit/push/merge/release/deploy、不 GPU、不改其他服務、不讀私密檔/credential/OTP、不國網 DNS/SSH/SFTP/登入/submit/額度消耗。工程通過後交使用者驗收，不自行開始真機階段。

## 內部 API（v1 路由保留）

`/api/projects` GET/POST；`/api/projects/{project_id}` GET/PUT `{revision,config}`；`/files` POST `{revision,files:[{path,data_base64}]}`：原子新增版本並整份更新程式快照，明示排除。`/example` POST `{revision}`：只空專案的明標範例。

`/api/environments` GET/POST `{name,sif_path}`；`/api/locations` GET/POST `{name,kind,path}`，無改遠端或本機讀取。

`/api/work-drafts` GET/POST `{project_id}`；`/api/work-drafts/{draft_id}` GET/PUT `{revision,spec,location_ids}`；`/prepare` POST `{revision}`；`/api/jobs/{batch_id}/clone` POST `{}`。回傳草稿原 v1 `{id,revision,spec,files}` 加 `snapshot`；spec 完整沿用 v1 欄位。Job record 加 `origin:project`、`snapshot`，舊無此欄位判為 legacy；原 job result/demo/cancel 路由保留。

Project `config={name,working_dir,entrypoint,arguments,environment_id,location_ids}`；預設工作目錄空字串表示專案根，入口相對專案根。Project 回傳 `id,revision,config,files,exclusions`。工作 snapshot 包含 `project_id,project_revision,project_name,working_dir,entrypoint,environment,locations`，位置/環境皆 `verified:false`。建立草稿時 spec.entrypoint=`code/`+入口，arguments 繼承預設；UI 以入口顯示摘要即可。資料位置/環境 list 離線先空，環境選空代表尚待設定。
