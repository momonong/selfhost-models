# Scheduler v1 CPU evidence

這一組證據對應 `feat/single-host-scheduler`，起點
`9ae9ff37cd47511ccd509a38b91111721a490a07`。程式、image、Linux snapshot 各自
以檔案完整 SHA256 對應；建置時為未提交來源，不能只靠 base HEAD 判斷 image 內容。
Manifest記錄實際測試／build工作目錄的原始bytes；Git的LF/CRLF正規化可能使另一次
checkout的原始hash不同，須分辨換行差異與程式內容改動，不能直接抹除歷史hash。

| 證據 | 範圍 |
|---|---|
| [windows-cpu.json](windows-cpu.json) | 164 項完整 CPU 契約測試與47項新增測試 |
| [linux-cpu.json](linux-cpu.json) | 72 項 Linux CPU 測試、production execute／真 decoder／TCP、Windows HTTP CLI、13次管理命令 |
| [linux-source-sha256.txt](linux-source-sha256.txt) | Linux72項驗證來源；其後僅 scheduler文件補充，程式／測試／鎖檔不變 |
| [whisper-assets.json](whisper-assets.json) | 固定 revision 的11個檔案、完整 SHA256與官方 metadata来源 |
| [images.json](images.json) | 最終 Whisper CPU image、source manifest、runtime/processor/relay結果；Qwen候選未建成 |
| [incident.json](incident.json) | Docker daemon EOF/500、建置停止點與現役 ready逾時；根因未知 |
| [重現包](reproduce/README.md) | 原生 Linux/WSL scratch上的 CPU-only 驗證，Windows只用HTTP |

合成 fake-clock、SQLite fault injection、實際 CPU process、Docker CPU 與真 GPU 是
不同證據。這組**沒有** GPU model load/inference、GPU資源釋放、ASR辨識品質或人工
驗收結果。既有 fixed runtime 的 processor/meta-device/EOS wrapper probe 亦不是
實際權重推論。D另見[待核定窗口](../../docs/scheduler-gpu-acceptance.md)。

重現包會產生新的隨機 fixture keys與合成 state；交付包不含 keys、SQLite、權重、
private host inventory。原始本機 logs/inspect與CPU scratch為追溯而保留，fixture
程序／Docker測試容器／測試network／測試volume已按各次receipt清理，未刪候選images
或模型資產。Daemon異常後沒有繼續Docker操作，也沒有由此推定其他容器退出。

另有 orchestrate 的獨立核對：相同 Qwen asset/revision/runtime、僅 context2048→4096
的兩個 deployment，實際 fake controller順序A1→A3→B2且發生兩個不同deployment load；
Whisper11檔完整SHA與固定revision官方metadata也已重算。這些是CPU／資產核對，不是GPU。

## Image 重現前置

先用 `docker image inspect` 確認 Dockerfile 的固定 base digest 已在本機，再以獨立
candidate tag 執行 `docker build --pull=false -f docker/whisper.Dockerfile ...` 或
`docker build --pull=false -f docker/worker.Dockerfile ...`。缺 base 時停止；不要讓
重現步驟意外變成大型下載。不要覆寫現役／release tag，也不要以此說明當服務恢復授權。

CPU probe 必須 `--runtime=runc --network none --read-only`、沒有 `--gpus`／device
requests，並明確限制CPU/RAM。Qwen可用 `--entrypoint python3` 匯入 `identity` 與
`worker.relay`，核對 `/opt/selfhost/identity.py`、`launch.py`、`worker/relay.py` 完整
SHA256。Whisper對 `/app` 的來源與固定runtime做同樣核對，processor僅用CPU、模型
僅用meta device；禁止呼叫 `WhisperEngine` 建構子或model generate，以免變成GPU驗收。
此次完整執行腳本／raw receipts留在ignored `.state/scheduler-asr-preparation`；
daemon故障後未執行的Qwen步驟是 `build_check_qwen.py`／`probe_qwen_cpu.py`，尚無成功證據。
