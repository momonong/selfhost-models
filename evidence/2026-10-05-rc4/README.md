# 2026-10-05：0.3.0rc4 路由與來源版本檢查

本輪只改文件入口／參考版本契約及 package 版本。起點 `83109ed6ea87a2f6c7d198c4a35c09f1f75f1387`；Linux／Python 3.12.3／uv 0.11.21，沿用既有暫存建置快取，未下載全套依賴或修改現役環境。

- [檢查 manifest](checks.json)：本機 selected commit 全文、dirty 不影響已提交文件、指定舊版優先於 HEAD、缺 commit／全文停止、GitHub main 解析與同 commit 三份全文／blob 一致性、相對連結／anchor、固定 playbook 路由與 lock／runtime／wheel metadata。
- [wheel 建置輸出](wheel-build.txt)：離線建置 rc4，31 個 wheel 檔案與來源一致；metadata 為 rc4、entrypoints 不變。此檢查不是 installed CLI／服務功能驗收，wheel 未發布。

GitHub 來源 walkthrough 在已發布的 rc3 commit 核對指南、樣本與個人筆記三份全文，manifest 明記 commit 與範圍；本輪更新指南推送後再以 PR 實際 head 唯讀核對，結果留於交付報告。這些檢查沒有國網 DNS／SSH／SFTP／job／GPU 操作。

未變 runtime 的依據是 30 個非版本模組／靜態檔與 rc3 雜湊相同、uv.lock 只改根 package 版本。重用 [rc3 CPU 380 passed／2 skipped](../2026-10-05-rc3/pytest.txt) 及 [rc3 已記錄的 UI／瀏覽器重用範圍](../2026-10-05-rc3/README.md)，不是新的 rc4 全套測試。歷史 [rc2](../2026-10-03-rc2/package-check.json) 收據保留，不把原版本結果改標 rc4。

暫存 wheel／GitHub 全文核對材料留在本機 `/tmp`，不納入 Git、不供部署。未新增永久 mirror tests、依賴、平台或服務；沒有部署／Release／images／訓練驗收聲稱。正常 GitHub checks 另核對本 PR head。
