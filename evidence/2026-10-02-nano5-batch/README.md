# Nano5 離線批次介面：工程驗證紀錄

本機離線工程交付已完成，等待使用者驗收。沒有登入、連線、上傳或提交真實 Nano5；沒有 GPU、模型載入或真實訓練。所有瀏覽器匯入檔案皆為本輪合成資料，使用者程式沒有被執行。費率/partition 的公開官方依據與操作方式見 [功能文件](../../docs/nano5-batch.md)。

## 版本與範圍

- 工作目錄 `/home/ubuntu/projects/selfhost-models`；單一既有 worktree。
- 分支 `feat/nano5-batch-ui`；HEAD `e1f3c2e6b3a2b6744142cb037b3e63a35d2c4f39`。
- 變更為獨立 CLI、ASGI app、SQLite store、原生前端、CPU 契約測試、文件及本紀錄。僅 pyproject 新增 CLI/package data，依賴與 uv.lock 不變。
- 尚未 commit、push、merge、release 或 deploy；現役 API、executor、Docker/GPU、網路入口未變更。
- 授權與 main/orchestrate/task/reviewer 對話 ID、固定指南版本見功能文件的開發追溯。

## CPU 與套件驗證

| 檢查 | 實際結果與證據 |
|---|---|
| 完整 CPU 契約 | `uv run --locked pytest -q`：**343 passed、2 skipped，67.42 秒**，見 [完整輸出](pytest-final.txt)。跳過項目保留原專案條件，不列為已通過 |
| 最後狀態訊息修改 | 完整測試後僅調整已有成果的成功事件文案；兩項結果保存測試 **2 passed、50 deselected**，見 [針對性輸出](pytest-result-preservation.txt) |
| 最終 wheel | [建置輸出](wheel-build.txt)；wheel SHA-256 `f91a1ee2e0e65f9d49f8b726509e6826acf839ee50dc845719ad6591455c9d64`，位於本機 `/tmp/nano5-wheel/`，未發布 |
| 全新環境 | Python 3.12.3、locked 依賴安裝於 `/tmp/nano5-clean-venv`；安裝最終 wheel 後從 `/tmp` 執行，六個程式/靜態檔 source = wheel = installed；三條靜態路由 200，unknown→匯入→確認成功仍保留成果 bytes，見 [環境與檔案雜湊](wheel-clean-env.txt) |
| 離線限制 | `nano5-batch --verify-offline` 實際確認 DNS 與 outbound connect 在系統呼叫前被 audit hook 拒絕。這是防禦措施，不是 OS sandbox；沒有真的對 Nano5 發出探測 |

本功能共 52 項 CPU 測試涵蓋資源/成本、路徑與 shell quoting、ZIP/記憶體限制、樂觀版本、immutable 工作包、unknown 不重送、取消不證明停止、成果來源/不可覆寫、中文下載名、HTTP Host/Origin/CSRF/CSP、CLI 與 guard。內建兩個可信範例曾在隔離 Python process、CPU/記憶體/檔案/時間限制下驗證可執行；不是匯入使用者程式，也沒有 GPU。

初次受限沙箱測試中，guard 測試因沙箱不允許建立 socket 失敗；正式批准後的 CPU 執行通過。本機舊 PATH uv 0.9.5 不符合專案要求，實際使用 `/home/ubuntu/.hermes/bin/uv` 0.11.21。uv cache 使用 `/tmp/nano5-uv-cache`，不改共用 cache。

## 審查與修正

獨立 reviewer 的首輪查出三項問題，均已修正並加入回歸：LZMA ZIP 小檔可要求巨大 decoder dictionary → 在建立 decoder 前僅允許 stored/deflate；孤立 Unicode surrogate 路徑 → 明確 422；舊畫面刪檔競態 → revision + SHA-256 交易核對。

main/orchestrate 另外核對內建範例 NameError、手動成果在稍後成功時被改回 pending、成功事件誤寫「尚未收集」，均已修正；成果 bytes、來源與下載可用性保留。下載 Content-Disposition 保留中文 basename/副檔名，並維持安全 ASCII fallback。

**獨立 reviewer 的最終複核是靜態審查，回報沒有新增 P1/P2；未獨立重跑動態驗證。** 上表測試、wheel smoke 與下列瀏覽器操作為實作 task 所執行，不將 reviewer 引用這些結果描述成第二份獨立動態證據。

## 真瀏覽器操作

使用 Codex in-app browser 對獨立 loopback 預覽操作。初次下載事件等待工具異常阻塞約 6 小時 53 分，導致錯過原訂 08:00 時間；其後以支援的有界 `downloadMedia` 完成實際下載與 SHA-256/ZIP CRC 核對，沒有重派任何未知工作。瀏覽器頁面錯誤/警告紀錄為空。

| 流程 | 可檢查證據 |
|---|---|
| 空狀態與輸入錯誤 | [空畫面](01-empty.jpg)、[缺少程式](02-missing-program.jpg)；dev 121 分鐘明確拒絕並保留輸入 |
| Python 工作包與成果 | [準備工作包](03-prepared.jpg)、[固定 CPU 成果](04-python-results.jpg)；success 先 pending，再收集為 available |
| 訓練準備 | [2 GPU／60 分鐘、公開學術費率估算 100 元](05-training-prepared.jpg)；未真訓練；unknown 情境後取消仍 unknown |
| 手動成果與文字安全 | 合成 ZIP 匯入 unknown 工作後仍 unknown/available；明確 fixture 確認後保留 manual_import bytes；[HTML 純文字預覽](06-imported-html-text.jpg)，無 script/svg 注入 |
| 窄螢幕 | 390×844：[工作頁](07-mobile-jobs.jpg)、[準備頁](08-mobile-compose.jpg)。頁面無水平溢出；工作清單內部捲動。驗證後還原 viewport |
| 實際 file chooser | [匯入中文 .py 與 CSV](09-uploaded-files.jpg)，只保存資料、未執行 |
| 最終重啟頁面 | [最終工作室](10-final-workspace.jpg)，重新載入更新 CSRF，保留原草稿/工作/成果 |
| 真實下載檔案 | [下載大小、ZIP entries、SHA-256](browser-downloads.txt)：工作包、合成成果 ZIP、中文單檔；已檢查內容與 CRC |

03/04 等早期截圖保留原驗證狀態；最終 UI 將真機阻擋細節折疊、下載優先，明顯顯示容量限制。審查修正後的成果保存另有 CPU 與最終 wheel 證據。

## 預覽與交接

最後觀測於 2026-10-02 09:46 臺灣時間：[HTTP、PID、埠、保存紀錄](preview.txt)。

- 預覽 `http://127.0.0.1:18771`，PID **1296418**，獨立 state `.state/nano5-batch-preview`（Git ignored）。4 草稿、3 工作，成果均可用；沒有自動輪詢。
- 現役 18080 PID 493831、18091 PID 493128、其他 18085 PID 327581 仍在 listening。本輪沒有停止或重啟這些程序；不將 listening 描述成其完整健康驗收。
- 預覽維持開啟供驗收，沒有建立 autostart 或背景監工。PID/存活是上述時間的觀測；若退出，先確認 18771 空閒，再沿用同一 state 啟動：

```bash
uv run --locked nano5-batch --state .state/nano5-batch-preview --port 18771
```

本版單包 16 MiB／100 檔、state payload 256 MiB／100 草稿／100 工作；大型 dataset/checkpoint、真實 SFTP/SSH/Slurm/account/SIF/權限、帳務、GPU 與訓練仍未驗證或實作。下一個必要動作為使用者驗收離線流程；真機測試須依功能文件另行確認範圍，不從此交付推定授權。
