# 2026-10-05：0.3.0rc3 文件與來源驗證

本輪範圍為跨研究 repo 國網參考文件、入口與 package 版本；起點 `af64c0649152ddc20961cb0211f849923ae1c6f5`。使用 Linux／Python 3.12.3／uv 0.11.21，暫存驗證與 wheel 環境均獨立於現役服務。

- [locked CPU 輸出](pytest.txt)：**380 passed、2 skipped，71.15 秒**。以 `UV_PROJECT_ENVIRONMENT=/tmp/selfhost-models-rc3-test-env` 執行 `uv run --locked --cache-dir /tmp/selfhost-models-rc3-uv-cache pytest -q`；uv 準備固定依賴後完成全套，跳過不算通過。
- [wheel 建置輸出](wheel-build.txt)：離線建置 `selfhost_models-0.3.0rc3-py3-none-any.whl`；未發布。
- [套件檢查](package-check.json)：從 repo 外以隔離 Python 核對版本／console entrypoint metadata，以及 31 個 source = wheel = installed 檔案。wheel SHA256 `b1f97bb2010a2a04ec70625bab194c133b6768f5da84e54babcea778dc0907b1`。metadata-only 環境以 `uv pip install --offline --no-deps` 安裝；本輪不將這項檢查描述成 installed CLI／服務功能驗證。
- [文件檢查](documentation-check.json)：相對連結／anchor、Bash 語法、未填路徑／Job ID 拒用、局部腳本內容 guard 與 dry walkthrough。局部 fixture 在 `/tmp` 審查文字，未在 `/work` 或遠端執行。

`uv.lock` 除根 package 版本外逐項相同，30 個非版本 runtime／靜態檔與 rc2 雜湊相同。前輪 [rc2](../2026-10-03-rc2/package-check.json) 的 installed CLI、ASGI、離線流程及 [v2 瀏覽器證據](../2026-10-02-nano5-v2/README.md) 依原版本／未變行為範圍重用；本輪未開瀏覽器、預覽、服務或重新動態驗 UI。

環境準備紀錄：舊 uv 快取缺少解析 metadata，離線重鎖失敗；改用獨立 `/tmp` 快取查核既有固定套件，重鎖結果只更新根 package 版本。`av` 首次下載等待約六分鐘，pytest 未開始時不計通過；停止前的核對發現下載已完成並已開始測試，因此沒有終止或切換程序，最終使用上述新 CPU 暫存環境完成全套。wheel 在建置工具快取備妥後離線建置。

公開官方 web 文件的查核日與來源見 [指南](../../docs/nchc-codex.md#官方查核與當次限制)。本輪未連線國網、未執行 GPU／訓練；CPU 與文件驗證不能證明研究依賴、SIF、多節點、所有 OS 或人類研究驗收已通過。正常 GitHub checks 另核對 PR head；本 repo 沒有 GitHub Actions workflow 的測試／建置聲稱。

暫存 wheel／metadata-only venv／CPU venv 留於本機 `/tmp` 作證據核對，不納入 Git，不供部署。未更新現役環境，未發布 tag／Release／images 或部署；歷史收據保留。
