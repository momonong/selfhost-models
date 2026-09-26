# 可分離的單機控制端與 GPU 執行端 v1

狀態：本階段 CPU／隔離／真 GPU 工程驗收完成；[結果與邊界](../evidence/2026-09-23-split-execution/README.md)。
起點 `fa35ec19f8512e869d9db1fa73159b8e6bba5bed`。
本階段由 main2 交付，沿用同一 checkout；允許本機提交與受控服務切換，
不合併、推送或發布 images。最新授權允許自主安排本專案停服與 GPU 故障驗收。

## 目標與邊界

控制端持有公開 API、SQLite 工作／attempt／lease／結果及排程政策；執行端持有
Docker、GPU、模型實體路徑與自己的持久命令 journal。兩端只經版本化 HTTP 協定
交換資料，執行端不開控制 DB、不讀控制端檔案。先在同一 Linux 主機驗證兩個
獨立程序與檔案隔離；不宣稱已通過多機、LAN、雲端或產品品質驗收。

保留公開 Chat／SSE 與 durable job 契約、deployment ID、固定模型 revision、
image digest 與官方 runtime。只使用已有 locked 依賴。控制 admission 是共同
容量與預算權威；執行端的 grant 驗證只是額外安全防線，不另建公開排程池。

## 內部協定與恢復

- 固定 control／executor 身分、協定版本、controller fencing epoch、engine grant、
  command ID、attempt ID 與 payload hash。每次外部執行前先持久保存 intent。
- 相同命令重送查原狀態；同 ID 不同內容拒絕。回應丟失、逾時、重啟不自動重派。
  已 executing 而無 terminal receipt 的命令保留 unknown。
- load／warmup／unload 與生成都需可查詢的命令。更高 fence 阻擋舊控制者新操作，
  但不代表舊 engine 已退出；退出證據必須對應原 engine 身分。
- executor 持久保存 terminal／result，控制端驗證原 attempt 身分並持久保存自己的
  receipt 後才 ACK。結果儲存失敗只重試儲存；保留 dedup 記錄，不能變成第二次推論。
- SSE producer 不依附 consumer 連線存活；斷線／取消／逾時只停止交付，直到 terminal
  或確定整個 engine 退出才釋放 lease。協定 404／409 不算 worker 已拒絕執行。
- asset_ref 仍為完整 manifest hash；控制端只保存 manifest，executor registry 解析
  本機 path 並驗證權重。音訊／影片／結果經有界資料傳輸，不依賴共同路徑。
- migration 為增量 schema，先用 SQLite backup 及 referenced artifacts 的副本驗證，
  不直接讓候選程式開現役正式 DB。保留原部署、權重、state、journal 與歷史證據。

## 驗證與部署順序

先通過 CPU 契約、命令重送／回應丟失／fencing／重啟／receipt 恢復、legacy 與 durable
共同 admission、真實分程序與檔案隔離，再進行真 GPU。GPU 前在本地計畫保存精確候選、
案例、有限時間與 load／generation 預算、停止條件、回復命令及當下資源／請求盤點。
新窗口不能沿用歷史已關閉窗口；失敗／取消不退款、不刪 lease 或 journal。

GPU 覆蓋 Qwen 文字／SSE／圖／工具／合成影片、Whisper bytes 傳輸、跨部署切換、
取消／斷線、控制／執行端重啟與未知結果隔離。只操作本專案資源，不動 host driver、
全域 Docker 或其他服務。工程驗收全過才保留候選，否則安全回復原可用版本。
最終交付精確 SHA、證據、操作文件、服務狀態、回復方式與未驗證項目。

## 操作介面

`modelctl --state <control-state> scheduler bind-executor --authority <control-id>
--executor <executor-id> --url http://127.0.0.1:18090 --key-file <control-secret-file>`
只允許 API/controller 排他鎖可取得、unloaded 且沒有待執行工作／lease 的離線 state。
兩個 ID 為操作者固定的 8–80 字元識別，不隨程序重啟改變；內部 key 必須與公開 API key
不同，以各端自己的私有檔案提供。state 不要放在共同 mount。

執行端先以 `scheduler executor-init --authority ... --executor ... --key-file ...
--config <scheduler-config.json>` 初始化獨立 state。再用
`scheduler executor-register --path <existing-model-path> --file <deployment.json>
--manifest <full-manifest.json>` 核對已存在權重，沒有任何自動下載。
控制端用 `scheduler asset-import --manifest <full-manifest.json>` 與原 `scheduler register`
登錄同一 deployment；模型絕對路徑不經協定回傳。

`scheduler executor --port 18090` 只綁 loopback。控制端仍使用原 `scheduler api`、
`scheduler controller` 與公開 HTTP jobs/Chat/SSE；binding 存在時自動使用 remote provider，
不存在時保留原 local provider。啟動先 executor，再 controller/API；停止先封住客戶端，
停止 API、核對 drain，停止 controller，使用相同 control state 的 `scheduler unload`，
確認 engine exit/retire/gate release 後才停止 executor。只停任一程序不等於 GPU 退出。

協定 journal 上限為 10,000 個命令與 1 GiB 邏輯保留量；滿載拒絕新命令，不刪 dedup
記錄來換容量。此版本 ACK 後仍保留 receipt／stream spool，長期 retention/GC 尚未提供；
SQLite/WAL 實體空間另需監控。未確定的 load/execute 不重跑；已保存整 engine 退出證據後，
新 fence 可繼續只清理已退出容器的 retire，並保留先前失敗命令。

API 與 controller 只需控制 state、内部 credential、HTTP 連線；不需 Docker socket、
worker key 或模型權重。executor 需要自己的 journal/key、已有權重唯讀路徑、原生 Docker。
CPU isolation fixture 使用 Linux Landlock 對兩個真實子程序限制檔案存取；fixture 不載入 GPU。
