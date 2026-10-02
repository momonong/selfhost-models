# Nano5 v2 本機離線交付

專案設定一次 → 重用建立工作 → 查看狀態/成果/複製。工程交付完成，等待使用者操作驗收；沒有真實國網連線、Slurm 排隊/提交、GPU 或訓練。規格與授權見 [v2 規格](../../docs/nano5-v2-spec.md)，操作契約見 [使用文件](../../docs/nano5-batch.md)。

## 版本、入口與驗證

- 目錄 `/home/ubuntu/projects/selfhost-models`，分支 `feat/nano5-batch-ui`，HEAD `e1f3c2e6b3a2b6744142cb037b3e63a35d2c4f39`；原 v1 dirty 成果沿用並演進，單一既有 worktree。尚未 commit/push/merge/release/deploy。
- 最終預覽 `http://127.0.0.1:18771`，PID **1350277**，state `.state/nano5-batch-preview`。最後記錄時間 2026-10-02 10:36 臺灣；[cmdline、host namespace、listen 與既有服務](preview.txt)。18080/18091/18085 原程序仍 listening，沒有操作它們；這不宣稱其完整健康驗收。
- 啟動：`uv run --locked nano5-batch --state .state/nano5-batch-preview --port 18771`。實際使用 uv 0.11.21 `/home/ubuntu/.hermes/bin/uv`，Python 3.12.3。CLI 只能 loopback，未建自啟或背景監工；若退出，先確認埠空閒再沿用 state。
- 完整 CPU：[**380 passed、2 skipped，70.97 秒**](pytest-final.txt)。2 跳過未列為通過；先前 [370/2](pytest.txt) 是新增排除測試前的結果。最後僅增加工作詳情參數/費用文字摘要，`node --check` 與真瀏覽器核對通過，Python/API 契約未變。
- 最終 [wheel 建置](wheel-build.txt)、[全新 locked 環境與 7 檔雜湊](wheel-clean-env.txt)：source = wheel = installed；靜態路由 200，project/snapshot/clone、unknown→匯入→成功保留原成果通過。wheel SHA-256 `426e991e096c8e8e05e85fbf92d682e44492ca5efd1358a008f4d87c4fdf77a6`，本機 `/tmp/nano5-v2-wheel/`，未發布。
- installed CLI `--verify-offline` 通過，DNS/connect 在系統呼叫前被拒；沒有實際國網探測。原沙箱 guard 測試仍因禁止 socket 建立失敗，正式批准本機 CPU 後完整通過，沒有降低 guard。

## 真瀏覽器與資料保留

一次選 `/tmp` 合成完整資料夾：main、子模組、相對 config 共 5 檔；4 個環境/秘密/快取佔位檔逐項排除，內容未讀取/傳送或保存。未選 SIF 也能完成首次匯入和工作準備；第二份工作直接重用專案與資源範本，不重新選檔。

專案更名/預設參數/環境/資料位置/程式更新至 v4，前兩工作仍是 v2；包雜湊、程式 manifest、參數、snapshot 完全不變：[修改前](browser-before-project-edit.json)、[最終逐項核對](final-browser-and-preservation.json)。複製產生新的 v2 草稿，沒有重送舊意圖；另以 v4 建立 H200 範本工作，metadata 均 `verified:false`，runtime 生成 README、cwd、唯讀 asset bind 核對通過。

手動匯入合成結果後仍 prepared/available，不假稱已運算；文字預覽與 [實際下載 CRC/中文 bytes](browser-download.json) 通過。舊版區只列 4 個 legacy 草稿，新版草稿未混入；主列表沒有舊 fixture。瀏覽器 error/warn 為空。

- [結果流程](01-results.jpg)、[最終工作摘要與成果](04-final-workspace.jpg)。
- [390×844 工作草稿](02-mobile-draft.jpg)、[390px 專案流程](03-mobile-project.jpg)，DOM page/client width 均 375，無頁面水平溢出；viewport 已還原。

獨立 reviewer `01a0f8b5-16c6-7952-b828-45aea36fd00a` 完成**一般靜態複核**：未選環境 null、legacy 來源、入口/根目錄、排除路徑格式修正成立，無新阻擋。其非阻擋 README 建議亦完成並加入契約測試，不改已有工作包。**reviewer 未獨立重跑動態驗證**，上列 CPU/wheel/browser/migration 是開發 task 證據；不重做歷史被攔的攻擊性動態重現。

## 備份、遷移與回復

備份 `/home/ubuntu/projects/selfhost-models/.state/nano5-v1-backup-20261002-1015`；試副本 `.state/nano5-v2-trial-20261002-1015`。先核對本人 PID 停止 18771，再保存整份 state；[基線行數/雜湊](migration-baseline.json)與[試遷移](migration-trial.json)一致，才對[原預覽](migration-preview.txt)作 additive schema。最終再按原主鍵逐列比較：4 drafts、8 draft_files、3 jobs、9 result_files **所有原欄位/bytes 不變**，integrity ok、foreign_key_check 空，舊包 CRC/輸入 SHA/成果可讀。沒有刪資料或杜撰專案來源。

回復方法（本輪未實際回復）：

1. 核對當時 18771 的本人 PID/cmdline，再停止 UI；不要停止其他服務。
2. 保留現有 `.state/nano5-batch-preview` 整份不改。將 v1 備份複製至**尚不存在**的 `.state/nano5-batch-v1-restored`，勿覆寫現有目錄；v2 新資料仍留原 state。
3. 新隔離 Python 環境使用現有 `uv.lock` 安裝依賴，再以 `uv pip install --python <新環境Python> --no-deps --force-reinstall` 安裝備份內 `v1-wheel/selfhost_models-0.3.0rc1-py3-none-any.whl`（原 v1 SHA `f91a1ee2e0e65f9d49f8b726509e6826acf839ee50dc845719ad6591455c9d64`）。不把 repo 的 v2 source 覆回 v1，也不共用現役服務環境。
4. 指向新隔離環境，以 `uv run --locked --no-sync` 執行其 `nano5-batch`，state 使用 v1-restored、port 18771；從 repo 外執行並用 `--project` 指定此 repo，避免 Python source 混用。核對 v1 4 草稿/3 工作/成果後再使用。

## 限制與下一步

程式/結果 16 MiB／100 檔；state payload 256 MiB，最多 100 草稿/工作/專案/環境/位置，每專案 100 版本。大型 dataset/模型/checkpoint 只可登錄位置，搬運與大型成果取回未實作。SIF/runtime、partition/權限、路徑/bind、帳務及正式 transport 全部待人類在場的新階段驗證；現在 submit 永遠 403。取消不確認停止，unknown 不重送，manual_import 不確認 terminal，已有成果 bytes/來源不可覆寫。

下一步是使用者驗收本機流程，不自行開始真機或發布。主動跨對話回報曾被自動批准審查拒絕，理由為未認定本次傳訊有明確人類授權；本輪未換工具/途徑重送，成果依原約定留本 task。
