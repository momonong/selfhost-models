# 單機排程開發候選版：main2 交接

2026-09-23：本次授權是整理文件、CPU 驗證、合併／推送 GitHub，以及必要 Docker Hub
候選 image 發布。**GPU 工作維持暫停，完整 managed D 驗收未完成，未通過人工驗收。**
候選 image 不更新 `0.2.0`／`latest`，也不代表部署或恢復服務。發布結果與精確版本另記於
[發布證據](../evidence/2026-09-23-scheduler-publication/README.md)。

## 目標與契約

讓同一台主機的 Qwen3.5-4B／vLLM 與 Whisper-small／Transformers 在有限 GPU 資源下，
以持久工作佇列、可追蹤生命週期及保守的取消／恢復語義切換。業務 prompt、工具執行、
人工校正與產品品質不屬於共用 API。未授權多機、多租戶、GPU 並載或其他模型的新階段。

- [排程契約](scheduler.md)：jobs／attempts／leases、opaque refs、完整資產 hash、
  deployment 身分、urgent FIFO、安全邊界、有限 reuse／aging、共同 Chat／SSE admission。
- SQLite WAL／FULL 位於同機 Linux／WSL 原生檔案系統；Windows 僅當 HTTP client。
  API 不掛 Docker socket，host controller 用固定模板操作獨立 worker 與 CPU relay。
- worker epoch／controller fencing、持久 dispatch intent、結果 receipt 與 asset pin
  防止不確定工作重派；取消／逾時不釋放 GPU，須確認 terminal 或整個 engine 退出。
- 同 Docker daemon 的 ownership gate 協調 static／managed；heartbeat、零 lease 或
  舊 `ready` 紀錄均不代表目前資源可用。部署方式見 [服務監督](scheduler-service.md)。
- 原有 [client 契約](client-integration.md)、[backend](backend.md) 與 static 模式保留。
  Linux 原生 Docker 0.2.0 驗收已由遠端 main 的 `6c87816` 合入，與 managed 證據分開。

## 已確認證據與限制

| 時間／版本 | 已確認 | 不代表 |
|---|---|---|
| 09-20 scheduler CPU | [A/B/C、子程序、Docker CPU 邊界及 Whisper 資產](../evidence/2026-09-20-scheduler-cpu/README.md) | GPU 切換、效能或中文品質 |
| 09-21 11:33:59 Taipei 前 | [第一輪失敗及 static 恢復](../evidence/2026-09-21-managed-runtime/README.md)；原窗口已結束 | 現在仍為 static ready |
| 09-21 PID 修正 `2f7dec7` | [no-model NCCL/Gloo 診斷](../evidence/2026-09-21-managed-repair/pid-diagnosis.json)：128 觸及 cgroup PID 上限，256 完成群組初始化；舊 explicit 128 保持不變 | OOM 診斷或完整模型驗收 |
| 09-21 冷啟動修正 `40acd84` | 同一 worker 約 209 秒到 health ready，load 預設調為有界 300 秒；新輪完成暖機與部分 Qwen 能力 | 啟動速度保證、所有 GPU 相容性 |
| 09-21 12:24:40 Taipei 暫停 | repair ledger 2 loads／18 generations 保守預留；其中 12 為暖機上界、6 為 client，未退款；API／controller 停止，Qwen／relay 與 model pin 保留 | GPU 空閒或後續窗口授權 |
| 09-23 整合版 `89799fa` | Windows／Python 3.12 完整契約 `186 passed in 21.75s`；此後發布前僅文件／證據整理 | 新 GPU 或原生 Linux managed 驗收 |

部分 Qwen 能力包括 durable 文字、圖片、工具第一／第二步、影片與完整 SSE，合計 6 次
client generation。接續 SSE detach 收到 non-200，歷史 driver 未保存 status/body，
且 admission counter 未增加。`watch_scheduler` 的 ready 更新方式是**尚未驗證、未修正
的疑點**；不能認定它就是該 non-200 的根因。私人原始 evidence 留在 ignored state；
公開摘要不附 request、audio、key、DB 或完整 inspect。

## 待決策與停止條件

完整 ①③②、urgent／capacity、取消／disconnect、重啟恢復、Whisper 真模型及正式 managed
持續服務仍未完成；不因候選發布而補跑。後續先由 main2 與使用者決定續作或受控卸載，
再由 orchestrate 統一安排。任何 GPU 操作都須重新確認 owner、現役其他專案、engine
身分、lease／pin、GPU／host headroom，以及新的有界窗口和預算；不得沿用舊 RESOURCE GO。
模型 ready、HTTP 200 或 CPU 測試均不構成產品品質、人類驗收或穩定版承諾。

## 協調入口

- main2：`01a0ccaf-edbe-7ca1-8815-f96bc0373d0a`，取代舊 main1，負責方向與驗收。
- orchestrate：`01a0bd3e-95f5-7560-8259-2f00788796f6`，統一協調與回報 main2。
- 原 scheduler task：`01a0bd41-849b-76d1-8e93-83ec1c627911`，沿用原工作目錄及
  `feat/single-host-scheduler`，起點 `9ae9ff37cd47511ccd509a38b91111721a490a07`。

未纳入 Gemma 分支，未新增 worktree。未來支援更多模型與正式 managed 服務是待決策方向，
不構成本次執行授權。新的 main 應先閱讀此頁、精確發布證據，再透過 orchestrate 接續。
