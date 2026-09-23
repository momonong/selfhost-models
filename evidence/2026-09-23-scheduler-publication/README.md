# 2026-09-23 scheduler 候選發布

本次為 opt-in 開發候選發布；完整 managed GPU 驗收未完成且 GPU 工作暫停。
目標、契約、歷史證據與協調入口見 [main2 交接](../../docs/handoff-main2.md)。

發布中的精確 source、images、registry digests 與 stable 標籤核對，完成後記錄在此目錄。
不更新 `0.2.0`／`latest`，不部署／恢復服務；私人 DB、keys、audio、模型與完整 inspect
留在 ignored state。CPU 驗證不替代 GPU／產品品質或人工驗收。
