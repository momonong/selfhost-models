# 驗收方式

## 有界影片（2026-09-16，工作分支驗收）

後續版本核對：影片成果已於 `841132e1732d29e72458fd4b09d012b12e4e7792` 合併並推送 GitHub main。以下保留當時工作分支驗收狀態；後續 0.2.0 images 發布與驗證界線見部署文件。桌機請依本文的 Linux 桌機章節操作。

起點 `main@5a077e3883b3b2b6a0e817ff91f209ace6b15bd9`，`feat/video-inference`，沿用主要工作目錄，沒有額外 worktree。已和 PixelReceipt 接入任務協調排他維護，僅操作本專案 API/worker；不並載 backend、不改模型、不發布 image。[可行性與失敗紀錄](video-feasibility.md)；證據集中於 `evidence/2026-09-16-video/`。

重跑需已協調服務使用時段；以下會重建本專案容器及受控 pause/restart，不能對正在給其他產品使用的服務直接執行。每次改用新的 evidence 目錄保留失敗結果。

| 驗收層級 | 結果與證據 |
|---|---|
| CPU API／decoder 契約 | [113 passed](../evidence/2026-09-16-video/cpu-tests.txt)，uv lock check 通過 |
| 真實 GPU／MP4 一般與 SSE | [最終影片 API](../evidence/2026-09-16-video/api-video-delivery.json)：2/10/30/60 秒、SSE/usage/DONE、格式與超限、解碼 deadline、2×504＋2×429 超載、disconnect 與 cleanup 通過 |
| GPU 已輸出後取消／逾時 | [最終串流](../evidence/2026-09-16-video/video-stream-delivery.json)：先確認非空文字再 pause；error 無 DONE、保留 lease，恢復後歸零 |
| Linux 真實解碼子程序 | [最終 decoder](../evidence/2026-09-16-video/linux-decoder-delivery.json)：720p60/3600 幀約 2.08 秒；512MiB/10 CPU 秒等 OS limits 已讀回，timeout/cancel 後 child 與 tempfile 都清乾淨 |
| 共用重啟恢復 | [故障驗收](../evidence/2026-09-16-video/general-faults.json)：一般文字/SSE、context/認證/body、6×429＋2×504、API restart quarantine、worker 503、新 epoch 恢復通過 |
| 原有單圖／工具 | [回歸](../evidence/2026-09-16-video/image-tools.json)：紅色 PNG、function call 與工具回送 731 通過 |
| 實際來源／runtime | [runtime](../evidence/2026-09-16-video/runtime.json)：API／worker 來源與擷取時 checkout 逐位元一致；固定 vLLM/torch/processor 未換版 |
| 交付狀態 | [交付核對](../evidence/2026-09-16-video/delivery-checks.json)、[前](../evidence/2026-09-16-video/before.json)／[後](../evidence/2026-09-16-video/after.json)快照：ready、零 lease；四個 KaChing 容器 ID/started_at/狀態與模型 inventory 不變 |

最終 60 秒合成 API request 為 120×256²、4,399 input tokens；一般回應 4.567 秒、SSE 3.747 秒，finish_reason=stop；前一輪相同案例一般回應為 7.761 秒。這是暖機後、有 cache 重用可能的個別觀測，不是 SLA 或冷啟動 benchmark。可行性 worker 探測取樣到的 host VRAM 最高 16,063 MiB（約15.7 GiB），不能當作單一 request 的 allocator 增量。

共用重啟驗收先完成；其後只修正「影片 CPU 準備不消耗 GPU dispatch 後 drain 預算」，新增契約測試並在最終 image 重跑影片一般/SSE、串流故障、Linux decoder、圖片與工具。文字重啟路徑未變，沒有重跑不受影響的完整 restart 流程。runtime 的 git_head 是提交前的起點，git_status 與所有實際來源 SHA256 同時保存；不可只讀 git_head 誤認部署仍是起點程式。

交付時影片部署保持在 `127.0.0.1:18080` 運行，不再維持排他維護。臨時 Linux CPU 驗證容器已自動移除；沒有新 worktree。忽略的 `.state/video-fixtures/` 合成素材、實驗 Compose override 與本機 `selfhost-models-api:video-candidate` image 保留供重跑／核對，候選 image 沒有運行中的容器；正式部署使用一般 Compose 設定。沒有合併 main、推送或發布 image。

```powershell
uv sync --locked
uv run --locked pytest -q
uv run --locked python scripts/service_snapshot.py --output evidence/<run>/before.json
# 確認 inflight/detached/uncertain=0 並已交接使用需求，再停目前部署。
uv run --locked modelctl stop
uv run --locked modelctl serve Qwen/Qwen3.5-4B --video --context 8192 --max-inflight 2 --gpu-memory 0.60
uv run --locked python scripts/video_decode_probe.py --stress --output evidence/<run>/decode-probe.json
uv run --locked python scripts/video_decoder_check.py --output evidence/<run>/linux-decoder.json
uv run --locked python scripts/video_acceptance.py --output evidence/<run>/api-video.json
uv run --locked python scripts/stream_lifecycle_check.py --video .state/video-fixtures/synthetic-60s.mp4 --output evidence/<run>/video-stream-lifecycle.json
uv run --locked python scripts/acceptance.py --faults --output evidence/<run>/general-faults.json
uv run --locked python scripts/multimodal_smoke.py --output evidence/<run>/image-tools.json
uv run --locked python scripts/capture_runtime.py --output evidence/<run>/runtime.json
```

影片驗收使用合成 H.264：2/10/30/60 秒的 JSON、60 秒 SSE＋usage＋DONE、實際首尾 PTS/幀數、壞檔、16MiB／60 秒上限、解碼階段 deadline、drain 及 API tmpfs 清理。CPU 契約另覆蓋 AAC 音軌不處理、VFR/codec/dimensions/fps 拒絕、接收總預算、pre-admission 不解碼、解碼子程序 kill/wait、取消與錯誤清理。Linux decoder 證據包含真實 720p60/3600 幀，以及真實 OS 子程序逾時／取消／無剩餘 child／空暫存。

串流故障在收到非空生成文字後才 pause 本專案 worker，驗證 deadline error/無 DONE 與斷線後持有 lease，unpause 後 drain。共用故障入口另測超載、API restart quarantine、worker unavailable 503 與新 epoch 恢復；影片路徑沿用同一 journal。普通、單圖、工具的回歸分開保留。

固定模型 revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`、vLLM 0.29.0、Transformers processor 5.16.1、PyAV 16.0.1、context 8192、max_inflight 2、GPU fraction 0.60。60 秒是本次合成服務輸入上限；不能解讀為已驗證任意編碼檔案、Linux 實體桌機或桌球內容品質。未提供代表性桌球片段，精彩程度與快速攻防均未人工評估。

本次先前失敗也保留：圖片用的總像素預算讓影片降解析度；完整 120 幀被 2048 encoder cache 拒絕；巢狀 size 引發 upstream duplicate-key；首次 Compose tmpfs flow sequence 未加引號造成 `invalid mount path: 'nodev'`，已修正並加契約檢查。沒有以降低取樣、換模型或重設 journal 繞過。

## 筆電可用部署（2026-09-16）


本次從 `d971dab` 建立 `feat/local-service-delivery`，新增日常 client 與使用／桌機驗收文件；沒有修改 serving、依賴鎖檔、模型或 runtime 配套。透過一般 `modelctl serve` 路徑重建 API／worker，新的實際 image ID 與來源比對見本次 runtime JSON，不以舊 image ID 代表此次部署。

- [CPU 測試](../evidence/2026-09-16-local-use/cpu-tests.txt)：87 passed，包含 client 的 SSE error／截斷判定；不是 GPU 證據。
- Transformers：[一般／SSE 與錯誤](../evidence/2026-09-16-local-use/transformers-acceptance.json)、[取樣／能力限制](../evidence/2026-09-16-local-use/transformers-smoke.json)、[runtime](../evidence/2026-09-16-local-use/transformers-runtime.json) 通過，之後[正常停機](../evidence/2026-09-16-local-use/transformers-stopped.json)。首次 PowerShell 轉存中文有編碼問題，client 改為 UTF-8，後續由 vLLM 補驗中文串流；原始檔保留，不宣稱初次中文顯示正常。
- vLLM：[首次啟動未 ready](../evidence/2026-09-16-local-use/vllm-first-start.json) 保留。worker 日誌在暖機期間出現長時間 kernel JIT，client 等待 360 秒後失敗，API 保留未知 warmup lease；延遲的完整根因未確定。沒有刪除 journal 或放寬安全契約，使用 `modelctl restart` 取得新 epoch 後恢復。
- 恢復後 vLLM：[一般／SSE 與錯誤](../evidence/2026-09-16-local-use/vllm-acceptance.json)、[合成圖片／工具](../evidence/2026-09-16-local-use/vllm-smoke.json)、[中文串流](../evidence/2026-09-16-local-use/vllm-client-stream-recovery.txt)、[runtime](../evidence/2026-09-16-local-use/vllm-runtime-final.json) 通過。Ubuntu WSL 另以 Linux Python 執行同一 client 呼叫這個服務，回覆 [READY](../evidence/2026-09-16-local-use/wsl-client.txt)。
- [交付快照](../evidence/2026-09-16-local-use/delivery.json)：vLLM 在 `127.0.0.1:18080` ready、inflight/detached/uncertain 均 0，刻意保持運行；模型 inventory 與其他服務未變。權重大小／mtime／小檔 hash 核對不等於完整權重逐位元比對。

本次重驗一般部署、backend 切換、新 client 與實際暖機失敗後恢復；沒有重跑未變更 serving 的全部 pause／超載故障案例，先前整合與來源分支證據仍保留。未配置開機自啟，未實際重開 Windows；筆電喚醒且 Docker Desktop 運行是可用條件。LAN／公網接入、Linux 實體桌機、HF 實際下載仍未由本次驗證。日常操作見 [本機使用指南](local-use.md)。

## Linux 桌機實機驗收（待執行）

這是桌機尚未執行的驗收程序，不是 Linux 通過證據。先完成 [桌機交接入口](deployment.md#linux-桌機交接入口) 的來源、host、模型與獨立 state 準備。發布來源為 `29bec68`；依部署文件拉取並核對 0.2.0 固定 images。下列使用 `--no-build`；若改用原始碼建置，移除選項並另記新產物與差異。

先盤點 GPU／port／其他服務並安排排他維護時段。即使 ready，也不代表沒有產品正在使用服務；以下包含超載、pause/restart、解碼與暫存檢查。現有部署需先協調停機，不直接搶占。不得同時載入兩個 backend。命令於 repo 根目錄以 Bash 執行，每次使用新的 evidence 目錄；任何失敗先保存 logs 與狀態，不跳過後繼續宣稱通過。

### A. CPU 與主機基準

```bash
set -euo pipefail
RUN="evidence/linux-desktop-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN"
git rev-parse HEAD > "$RUN/source-commit.txt"
git status --short > "$RUN/git-status.txt"
uv sync --locked
uv run --locked pytest -q | tee "$RUN/cpu-tests.txt"
uv run --locked modelctl doctor > "$RUN/doctor.txt"
uv run --locked python scripts/service_snapshot.py --output "$RUN/before.json"
# 生成本機合成素材；不下載任何桌球影片。也產生 decoder check 所需的 720p60 fixture。
uv run --locked python scripts/video_decode_probe.py --stress --output "$RUN/decode-probe.json"
```

CPU 測試基準為 0.2.0 發布的 117 項；若來源後續變更，依實際測試與差異核對。解碼探測是 CPU 證據，不能當 GPU 驗證。`RUN` 變數需保留在同一個 shell；若換 shell，設回同一個本次目錄，不覆寫舊紀錄。

### B. vLLM 文字／圖片／工具與影片

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend vllm --video --context 8192 --max-inflight 2 --gpu-memory 0.60 --no-build
uv run --locked python scripts/chat.py --wait 600 --stream 'Reply with READY.'
uv run --locked python scripts/acceptance.py --faults --output "$RUN/vllm-general.json"
uv run --locked python scripts/multimodal_smoke.py --output "$RUN/vllm-image-tools.json"
uv run --locked python scripts/stream_lifecycle_check.py --output "$RUN/vllm-text-stream.json"
uv run --locked python scripts/video_decoder_check.py --output "$RUN/linux-decoder.json"
uv run --locked python scripts/video_acceptance.py --output "$RUN/video-api.json"
uv run --locked python scripts/stream_lifecycle_check.py --video .state/video-fixtures/synthetic-60s.mp4 --output "$RUN/video-stream.json"
uv run --locked python scripts/capture_runtime.py --output "$RUN/vllm-runtime.json"
uv run --locked modelctl stop
uv run --locked python scripts/service_snapshot.py --before "$RUN/before.json" --output "$RUN/vllm-stopped.json"
```

先確認 ready、模型 revision／backend／videos 正確。影片測試涵蓋 2/10/30/60 秒合成 MP4、一般與 SSE、usage／DONE、超限、解碼逾時、超載、斷線及暫存清理；decoder check 驗證 API 容器內的 OS limits、真實子程序 kill/wait 與清理。串流故障需在實際非空文字後才注入，確認底層未完成時仍持有 lease。若筆電的 8192 context／並行 2／GPU 0.60 無法在桌機初始化，保存失敗與 VRAM 紀錄，先協調調整；不能悄悄降低影片規格後宣稱原上限通過。

### C. Transformers 文字（必須先完成 B 的停止核對）

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend transformers --context 2048 --max-inflight 1 --gpu-memory 0.60 --no-build
uv run --locked python scripts/chat.py --wait 600 --stream 'Reply with READY.'
uv run --locked python scripts/acceptance.py --faults --output "$RUN/transformers-general.json"
uv run --locked python scripts/transformers_smoke.py --output "$RUN/transformers-smoke.json"
uv run --locked python scripts/stream_lifecycle_check.py --output "$RUN/transformers-stream.json"
uv run --locked python scripts/capture_runtime.py --output "$RUN/transformers-runtime.json"
uv run --locked modelctl stop
uv run --locked python scripts/service_snapshot.py --before "$RUN/before.json" --output "$RUN/transformers-stopped.json"
```

Transformers 不支援影片，不加 `--video`。若本次桌機只部署 vLLM，可以延後 C，但交接須明確標示 Linux Transformers 未驗收，不能以 vLLM 成功代替。

### D. 完成條件與失敗收尾

- 命令 exit 0，且 evidence 各檢查成功；一般／SSE 取得完整結果，能力限制與錯誤回應正確。
- 取消／逾時後 lease 保留到實際完成，恢復後歸零；decoder children 與暫存清理，模型／其他服務不受影響。
- 保存實際 commit、dirty state、模型 revision、image IDs、runtime、GPU／driver；重建結果不假稱與筆電 image digest 一樣。
- 若命令失敗，`set -e` 會中止後續命令，但不會自動完成停機。另開 shell 保存本專案 Compose logs，檢查容器是否 paused、lease 與就緒狀態；在協調好的維護範圍內恢復／停止本專案容器，不刪 journal、不停止其他服務。未回收子程序或未正常停止不能算通過。
- 最終驗收停機快照應有正常退出、未 paused、端口釋放。需要日常運行時依部署文件重新啟動選定 backend，取得 ready 並記錄最後狀態；測試完成和服務常駐是兩件事。
- Linux GPU 服務通過、HF 實際下載通過、桌球精彩程度品質通過分開回報；合成影片不能證明快速攻防判斷品質。

### 桌機任務開場文字

```text
請依 docs/deployment.md「Linux 桌機交接入口」及 docs/acceptance.md「Linux 桌機實機驗收」在這台桌機部署並驗收 selfhost-models。
先讀 AGENTS.md，核對實際工作目錄、HEAD、dirty state 與其他任務／GPU／Docker 使用情況。
影片來源基準為 841132e1732d29e72458fd4b09d012b12e4e7792；確認 checkout 包含此提交及本次交接文件，記錄實際 HEAD，不自行 reset。
發布來源為 29bec681f6577f88067edf8cca9952e66b58232c；checkout 須包含此提交。依部署文件拉取 0.2.0 固定 digests，使用 --no-build；若自行建置要另外記錄產物。
建立桌機專屬模型登錄與 state/key，不複製筆電 .state。優先完成 vLLM 文字／圖片／工具／影片，再完全停止後驗證 Transformers 文字。
可執行已協調維護時段內的本專案部署與故障驗收；驅動／Docker 主機級變更及模型下載若尚未授權，先列出具體需求。
若缺少硬體資源或相容性不足，保存失敗證據並討論，不替換固定 runtime 或悄悄降低驗收規格。
完成後保留 vLLM 影片服務 ready 供桌機本機使用，回報 key 檔案位置但不輸出內容。不開放 LAN／公網，不影響其他服務。
不合併、推送或發布新 images；如需修正程式，在工作分支完成並交代變更與驗證。桌球產品品質另行驗收。
```

## 整合交付（2026-09-16）

從 `main@fe3b5ba0c4159ef57a26da82819367cd95364d1c` 依序 merge Transformers `876593a70a2c05954270da262edc842b13a14391`、WSL `e3fb6edd04e76429f58b86600ab83ad913101c2d`，保留兩邊歷史與原始證據。整合分支為 `integrate/transformers-wsl-validation`，主要工作目錄 `D:/projects/selfhost-models`。兩次 merge 為 `8d154a6`、`5938a9a`；驗收工具後續修正為 `6c132a8`。

衝突位於 `capture_runtime.py`、`stream_lifecycle_check.py` 與本文件：合併 backend-aware `compose_command(state)`、URL/state/output、兩種 worker 來源檢查、raw SHA256／僅 CRLF→LF 正規化比較，並保留非空 `delta.content` 才注入故障的條件。API 與 worker 都要求來源檔案集合完全相同；內容、BOM、空白或最後換行漂移仍拒絕。新增部署預檢將 HTTP port、state 認證、Compose 設定、backend 與實際容器綁在同一部署，錯配先拒絕；同 image ID 的 digest 引用可接受，其他設定仍須一致。

相對 Transformers 指定成果，serving、schema、模型、依賴、Dockerfile 與 Compose 內容均未改。本次重用既有本機 image，以 `--no-build --pull never` 啟動，沒有重新 build／pull 或下載權重；因此只重跑受整合影響的 GPU 驗收，不重複來源分支已通過的超載及 API／worker restart 完整回歸。

| 層級 | 整合結果與證據 |
|---|---|
| Windows CPU | **84 passed / 5.22s**；[輸出](../evidence/2026-09-16-integration/cpu-tests-final.txt) |
| Ubuntu WSL2 CPU／CLI | **84 passed / 9.63s**；[最終測試](../evidence/2026-09-16-integration/wsl-cpu-final.txt)、[離線環境與 CLI](../evidence/2026-09-16-integration/wsl-cpu-cli.txt)；主目錄獨立 `.venv-wsl-integration`，不共用 Windows `.venv` |
| 固定依賴／範圍核對 | 兩份 uv lock 檢查、CLI、來源與歷史證據未變，[契約摘要](../evidence/2026-09-16-integration/contract-checks.json) |
| Transformers 真實 GPU | 新 state `.state/integration-transformers`、port 18082；[一般／SSE 與輸入限制](../evidence/2026-09-16-integration/transformers-acceptance.json)、[文字／取樣與能力拒絕](../evidence/2026-09-16-integration/transformers-smoke.json)、[串流 deadline／disconnect](../evidence/2026-09-16-integration/transformers-stream-lifecycle.json) 均通過 |
| vLLM 真實 GPU | Transformers 停止後才啟動，新 state `.state/integration-vllm`、port 18083；[一般／SSE 與輸入限制](../evidence/2026-09-16-integration/vllm-acceptance.json)、[圖片／工具](../evidence/2026-09-16-integration/vllm-smoke.json)、[串流 deadline／disconnect](../evidence/2026-09-16-integration/vllm-stream-lifecycle.json) 均通過 |
| runtime／來源 | [Transformers](../evidence/2026-09-16-integration/transformers-runtime.json)、[vLLM](../evidence/2026-09-16-integration/vllm-runtime.json)：兩 API 各 5 檔、Transformers worker 8 檔僅換行不同，raw match=false／text match=true；vLLM worker 逐位元相同。完整原始 hash、差異清單、image ID、model revision 均保留 |
| URL/state 錯配 | 三個相關入口共 6 次實際拒絕，[結果](../evidence/2026-09-16-integration/target-mismatch.json)；故障注入前拒絕，不操作其他部署 |
| 正常停機 | [Transformers](../evidence/2026-09-16-integration/transformers-after.json)、[vLLM](../evidence/2026-09-16-integration/vllm-after.json)：API／worker exit 0、port 關閉、leases 空、模型 inventory 與既有四個 KaChing 容器未變 |

模型仍為 `Qwen/Qwen3.5-4B@851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，唯讀掛載。GPU 執行環境仍是 Windows Docker Desktop WSL2／RTX 5090 Laptop；這不是 Linux 實體桌機或 Linux Transformers 的驗收。HF 實際下載、其他 GPU／driver、產品品質與強制 GPU abort 都未由本次證實。Transformers 仍為文字、單請求、queue 0、`drain_to_terminal`，能力限制見 backend 文件。

Transformers runtime 對應 `5938a9a`；vLLM runtime 對應 `6c132a8`。中間僅新增驗收預檢的同 image ID 引用相容與 `acceptance --faults` guard，既有正常設定驗證與 serving 未變；補做最終 CPU 與實際錯配檢查，未重跑不受影響的 Transformers 推論。完整命令列與退出碼見 [Transformers 命令](../evidence/2026-09-16-integration/transformers-commands.json)、[vLLM 命令](../evidence/2026-09-16-integration/vllm-commands.json)，本次原始日誌保留於 `evidence/raw/2026-09-16-integration/`。

WSL worktree 已經由 `git worktree remove` 清理，來源與整合分支保留。[清理紀錄](../evidence/2026-09-16-integration/worktree-cleanup.json) 包含 ignored 檔案盤點與 Windows Git symlink 清理故障／恢復過程。36 個 state／原始證據檔先另存至 `evidence/raw/2026-09-16-integration/worktree-preserved/` 並 SHA256 核對；WSL 原生 `/home/morris/.local/state/selfhost-models/vllm-acceptance-20260916` 保持原樣。下方各節為來源任務的歷史驗收，測試數量與當時部署狀態不代表當前整合版本。

## Transformers backend（Windows，2026-09-16）

Qwen/Qwen3.5-4B，固定 revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，已完成 Windows Docker Desktop WSL2／RTX 5090 Laptop 真實 GPU 文字推論與生命週期驗收。模型唯讀接入 `D:/hf_models/Qwen3.5-4B`，未搬移、覆寫或下載。這是合成服務驗收；不代表 Linux 實體桌機上的 Transformers、圖片／工具能力或產品品質通過。

共同起點 `fe3b5ba0c4159ef57a26da82819367cd95364d1c`，主工作目錄 `D:/projects/selfhost-models`，分支 `feat/transformers-backend`。推論與故障測試對應程式 `76fa5264bce055e39cfc6eda24e59a19063bdfff`；最終部署 `8b11685` 只補明確 stop grace period，並已重驗 readiness、一般／SSE 及正常停機。完整來源 SHA256、擷取時 commit、image ID 均在 runtime JSON，證據／文件的後續提交不代表重新建置 image。

| 驗證層級 | 結果與保留證據 |
|---|---|
| CPU 單元／契約 | [57 passed 與命令紀錄](../evidence/2026-09-16-transformers/contract-checks.json)；兩 backend 共用 admission、取消、未知 lease、串流與 warmup terminal 契約，另測真實 ASGI worker 搭配 CPU engine double；不當作 GPU 證據 |
| runtime 離線預檢 | Python 3.12.3、torch 2.13.0+cu130、CUDA 13.0、torchvision 0.28.0+cu130、Transformers 5.16.1；無網路／無 GPU 下成功匯入 Qwen3.5 類別並以既有 tokenizer 套用合成模板 |
| 真實 GPU 一般／SSE、輸入／認證上限 | [完整驗收](../evidence/2026-09-16-transformers/acceptance.json)，2026-09-16 05:18–05:19 UTC；400／401／404／413 均符合預期 |
| 超載、deadline、取消 | 容量 1、queue 0：7×429 + 1×504；保留 1 個 detached lease，完成 drain 後歸零 |
| API／worker 重啟 | API 重啟後 uncertain=1 且 not ready；worker 不可用回 503，新 epoch＋4 token 暖機後 ready=true、所有 lease 歸零 |
| 真實文字輸出後的 SSE 逾時／斷線 | [串流生命週期](../evidence/2026-09-16-transformers/stream-lifecycle.json)：只在非空 delta.content 後 pause；deadline 有 error／無 DONE，兩種情境保留 lease 至完成 |
| 文字與取樣 | [文字 smoke](../evidence/2026-09-16-transformers/text-smoke.json)：合成 2+2 回 4、一般／SSE 內容一致、同 runtime 固定 seed 重複取樣一致；圖片、tools、thinking、stop、非零 penalties 與 greedy top_p 均 400 |
| 來源／隔離 | [推論時 runtime](../evidence/2026-09-16-transformers/runtime.json) 及 [最終 runtime](../evidence/2026-09-16-transformers/runtime-final.json)：API 與 worker 的實際來源／uv.lock raw SHA256 均符合 checkout；worker 無 published port、模型唯讀、僅內網，API 無 Docker socket |
| 停機補驗 | [stop grace 修正後一般／SSE](../evidence/2026-09-16-transformers/shutdown-recheck.json)、[停止後快照](../evidence/2026-09-16-transformers/after.json)：兩服務 exit 0、port 18080 關閉、GPU 約 440 MiB、model inventory 未變、state 保留、四個 KaChing 容器未改 |

已保存兩個實測問題，沒有以失敗結果冒充通過：

- [暖機 cache 診斷](../evidence/2026-09-16-transformers/startup-diagnosis.json)：官方 PyTorch native bmm 走 Triton 時寫入唯讀 `/home/service/.triton` 失敗。改成限定 1 GiB 的可寫／exec `/runtime-cache`，未更換 runtime 配套；原始日誌與未決 warmup lease 的 SHA256 保留。
- [停機診斷](../evidence/2026-09-16-transformers/shutdown-diagnosis.json)：容器實際 StopTimeout=1 秒，Docker 依序送 SIGTERM/SIGKILL，得到 137 且 OOMKilled=false。明確設定 30 秒後重新驗證正常 exit 0。先前通過的推論／故障結果保留，僅補測受影響的收尾設定。

最終 Transformers worker 本機 image ID：`sha256:17895a2b87a9f3968eb6acf0d80c60e0e3dfa3a65c540daf5160a2cb3cfb5700`，API：`sha256:c4dd80bb466538ec90c4bef9d50e6f694e07fe3510e6fa6190f7ed8b716b3d9c`。這是本機建置的 content-addressed ID，未推送 registry。官方基底與 uv digest 固定於 Dockerfile；後續建置 attestation 可能造成 image ID 不同，必須以實際來源 hash 和 runtime 核對。

可重跑命令（先確定共用 GPU／容器無其他任務使用，`--output` 選新的證據目錄）：

```bash
uv sync --locked
uv run --locked pytest -q
uv run --locked modelctl doctor
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
uv run --locked python scripts/service_snapshot.py --output evidence/tf-rerun/before.json
uv run --locked modelctl serve Qwen/Qwen3.5-4B --backend transformers --max-inflight 1 --context 2048 --gpu-memory 0.60
uv run --locked python scripts/acceptance.py --faults --output evidence/tf-rerun/acceptance.json
uv run --locked python scripts/stream_lifecycle_check.py --output evidence/tf-rerun/stream-lifecycle.json
uv run --locked python scripts/transformers_smoke.py --output evidence/tf-rerun/text-smoke.json
uv run --locked python scripts/capture_runtime.py --output evidence/tf-rerun/runtime.json
uv run --locked modelctl stop
uv run --locked python scripts/service_snapshot.py --before evidence/tf-rerun/before.json --output evidence/tf-rerun/after.json
```

故障腳本 `finally` 會解除本專案 worker pause；若任一命令失敗仍須依狀態執行 `modelctl stop`，保留模型、state 與失敗證據。不要刪除 lease journal 以繞過復原契約。

### 本分支的 vLLM 真實 GPU 回歸

Transformers 已於 05:25 UTC 正常停止並確認 GPU 釋放後，才啟動 vLLM。`docker/worker.Dockerfile`、`worker/identity.py`、`worker/launch.py`、API `uv.lock` 相對共同起點均無修改。官方 vLLM 0.29.0／torch 2.13.0+cu130／CUDA 13.0／Transformers 5.16.1、V1 runner 配套不變。

- [完整 API／故障回歸](../evidence/2026-09-16-transformers/vllm-regression.json)：05:29–05:31 UTC，一般／SSE、錯誤輸入、6×429 + 2×504、取消、API 隔離及 worker 新 epoch 暖機恢復通過；暖機新增的 terminal＋至少 4-token 檢查也通過。
- [合成圖片與工具往返](../evidence/2026-09-16-transformers/vllm-multimodal.json)：紅色圖片辨識與 client 執行合成工具後返回 731 通過。
- [實際文字輸出後的串流故障](../evidence/2026-09-16-transformers/vllm-stream-lifecycle.json)：deadline／disconnect 保留 lease，drain 後歸零。
- [vLLM runtime](../evidence/2026-09-16-transformers/vllm-runtime.json)：本分支 API 來源／鎖檔 raw hash 符合 checkout、ready=true 且所有 lease 歸零。本機 worker image ID `sha256:f990b1866f6ae4f6f1063395001ddfa40076acf0085d6ecae5c78e7a12170630`；重建 attestation 會使 ID 不同，但官方基底、套件與 worker 來源未變。
- [整體最終狀態](../evidence/2026-09-16-transformers/final-state.json)：05:33 UTC，API／worker 均 exit 0、未 paused、18080 關閉；GPU 約 503 MiB、模型 inventory 未變、state 保留、四個 KaChing 容器的 ID／啟動時間／狀態未變。

重跑 vLLM 時，把上述 serve 指令改為 `--backend vllm --max-inflight 2`，並用 `scripts/multimodal_smoke.py` 取代 Transformers 專用文字 smoke；一般與 stream lifecycle 兩支腳本共用。當前 `.state/compose.env` 保存的是最後回歸的 vLLM 設定，所有服務已停止；要啟動 Transformers，請明確重新執行 `modelctl serve ... --backend transformers`。

## 筆電 Ubuntu WSL2 複驗（2026-09-16）

依本次安排，在既有 Windows 筆電的 Ubuntu 24.04.3 WSL2 執行 Linux CLI 與真實 GPU 驗收。**這不是 Linux 實體桌機驗收**；daemon 仍為 Docker Desktop 4.50.0／Engine 28.5.1，kernel 為 `6.6.87.2-microsoft-standard-WSL2`，RTX 5090 Laptop 24 GB／Windows driver 581.57。

來源為 `fe3b5ba0c4159ef57a26da82819367cd95364d1c`，分支 `test/linux-vllm-acceptance`。因 Transformers 任務同時使用主要目錄，本次使用獨立 worktree `.worktrees/linux-vllm-acceptance` 並先協調 GPU 使用時段。此輪修改兩支驗收腳本的 `--url`／`--state`、runtime 證據欄位及來源換行比對，新增來源漂移回歸測試；共用 API、Compose、worker、uv.lock 皆維持基線。

使用與先前 Windows 證據**完全相同的應用程式 image**，以 digest override、`--no-build --pull never` 啟動：

| 項目 | 固定值 |
|---|---|
| API image ID／RepoDigest SHA | `sha256:3ec4784560e022bdcf50ef655164b61985e026e480a0ebda686237a7d8b3b69e` |
| vLLM image ID／RepoDigest SHA | `sha256:419b067bd636b368b7bcb714ac5b811c48defdd2f16cc7aaab62171228102173` |
| 官方 vLLM 基底 | `v0.29.0@sha256:c2914767605584b6d8f45686b82de173ecc99e781897aa3d0a66dacd72c51ae1` |
| 模型 | Qwen/Qwen3.5-4B，revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a` |
| Host 設定 | GPU 0、loopback port 18081、UID/GID 1000、原生 WSL state、key mode 600／runtime dir 700 |
| 推論設定 | BF16、context 2048、capacity 2、queue 0、GPU utilization 0.60、deadline 30s／drain 120s、V1 runner |

模型唯讀接入 `/mnt/d/hf_models/Qwen3.5-4B`，既有 HF local metadata 一致；沒有下載、移動、替換模型。啟動方式見 [固定 image 複驗](deployment.md#wsl2-路徑權限與固定-image-複驗)。本次 state 位於 `/home/morris/.local/state/selfhost-models/vllm-acceptance-20260916`。

可重跑指令（請使用新的 evidence 目錄，避免覆蓋本次結果；先完成前置盤點與服務啟動）：

```bash
STATE_DIR="$HOME/.local/state/selfhost-models/vllm-acceptance-20260916"
EVIDENCE_DIR="evidence/$(date -u +%Y%m%dT%H%M%SZ)-wsl2"
uv sync --locked
uv run --locked pytest -q
uv run --locked python scripts/acceptance.py --url http://127.0.0.1:18081 --state "$STATE_DIR" --faults --output "$EVIDENCE_DIR/acceptance.json"
uv run --locked python scripts/multimodal_smoke.py --url http://127.0.0.1:18081 --state "$STATE_DIR" --output "$EVIDENCE_DIR/multimodal.json"
uv run --locked python scripts/stream_lifecycle_check.py --url http://127.0.0.1:18081 --state "$STATE_DIR" --output "$EVIDENCE_DIR/stream-lifecycle.json"
uv run --locked python scripts/capture_runtime.py --url http://127.0.0.1:18081 --state "$STATE_DIR" --output "$EVIDENCE_DIR/runtime.json"
docker compose --env-file "$STATE_DIR/compose.env" -f compose.yaml -f "$STATE_DIR/images.json" stop
```

任何腳本失敗也需執行停機與最終狀態確認。故障注入腳本對其 pause 的 worker 使用 finally unpause；不要中斷整個執行程序或停止 Docker daemon。WSL 與 Windows 使用獨立 `.venv`，鎖檔未變；Windows 回歸設定 `UV_PROJECT_ENVIRONMENT=.venv-windows`。既有 Windows 證據保留於 `evidence/2026-09-16/`。

本次發現的來源比對問題：Windows Git 新建 worktree 使用 CRLF，而既有 image 的 `pyproject.toml`／`uv.lock` 使用 LF。初次 runtime 擷取因此非零退出，已保留 [原始命令結果](../evidence/2026-09-16-wsl2/commands.json) 及 [該輪停機狀態](../evidence/2026-09-16-wsl2/final-state.json)，不把初次擷取標成成功。

修正後仍保存 raw SHA256，另計算**僅將 CRLF 轉 LF**的 hash；`api_source_matches_checkout` 表示逐位元相同，`api_source_text_matches_checkout` 表示上述換行正規化後相同，差異檔名記錄在 `api_source_newline_only_differences`。不忽略 BOM、空白、最後換行、程式內容或來源檔案集合差異；這些仍非零退出。Windows／WSL 的 34 項回歸皆通過，詳見 [契約驗證](../evidence/2026-09-16-wsl2/contract-checks.json)。

| 本次驗證 | 結果與獨立證據 |
|---|---|
| 前置盤點、原 image、模型及權限 | [preflight.json](../evidence/2026-09-16-wsl2/preflight.json)；既有資產、不 build／pull，key 600、runtime dir 700 |
| 一般／SSE、錯誤參數／認證／body 上限 | 通過，[acceptance.json](../evidence/2026-09-16-wsl2/acceptance.json) |
| 超載、deadline、取消、API／worker 重啟 | 6×429 + 2×504；未完成 lease 保留、drain 歸零、API 重啟隔離、worker 缺席 503、新 epoch 暖機後 200 |
| 圖片與工具往返 | 合成紅色圖片、工具參數與回傳 731 通過，[multimodal.json](../evidence/2026-09-16-wsl2/multimodal.json) |
| SSE 200 後的 deadline／斷線 | error frame／無 DONE、保留 lease 到 terminal，通過 [stream-lifecycle.json](../evidence/2026-09-16-wsl2/stream-lifecycle.json) |
| 修正後 runtime 擷取 | [WSL](../evidence/2026-09-16-wsl2/runtime.json) 與 [Windows](../evidence/2026-09-16-wsl2/windows-runtime.json) 均通過；raw match=false、text match=true，9 個檔案僅 CRLF/LF 差異，原始 hash 全部保留 |
| 最終停機 | [recapture-final-state.json](../evidence/2026-09-16-wsl2/recapture-final-state.json)：API／worker exit 0、未 paused、18081 關閉、leases 空、模型 inventory 與其他容器未變；GPU 426 MiB used／23626 MiB free |

完整 GPU 功能組合完成於 05:02–05:04 UTC。來源比對修正後只補做所需的暖機與 WSL／Windows runtime 擷取，沒有以重跑覆蓋初次失敗。原始 logs 保存在 Git 忽略的 `evidence/raw/2026-09-16-wsl2/`，原始 preflight 及日誌 hash 見對應摘要。本次模型檢查為大小、mtime 與小檔 hash，並非完整權重逐位元驗證。GPU 已歸還後，後續任務可能重建同名 Compose 容器；以各次證據的 container ID、時間與 image digest 識別。

此歷史驗收的串流結果仍是基線「首個 data frame 後 pause」語義；整合後已採用非空 `delta.content` 才注入故障，整合結果另外保存。此輪沒有修改 backend 契約或 serving image；Windows 的新增來源回歸、CLI 與 runtime 擷取已補驗，沒有新增 serving image 的待補回歸。Linux 原生桌機、獨立 NVIDIA Container Toolkit 安裝、不同 GPU／driver 組合仍待實機驗收。

## uv 版 vLLM 歷史交付複驗（2026-09-16）

**最新 uv 版 API 與固定 vLLM worker 已通過完整真實 GPU 複驗，驗收後兩個服務均正常停止（exit 0）。** 本次交付程式起點為 `cea1d4a3dfb26edf2bf1b5a68bdc3dbac33dcb1d`；未修改 serving 程式、模型、Compose 設定或官方 worker 配套，僅補強證據腳本的輸出路徑及容器來源比對。

- 模型：Qwen/Qwen3.5-4B，revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`。Windows 11／Docker Desktop WSL2、RTX 5090 Laptop、driver 581.57。
- API：Python 3.12.14、uv 管理的 `/app/.venv`，正式環境不含 pytest；容器內 API 原始碼、pyproject 與 uv.lock 的 SHA256 與 checkout 全部相符。
- Worker：vLLM 0.29.0、torch 2.13.0+cu130、CUDA 13.0、Transformers 5.16.1，顯式 V1 runner。新建 image 的完整 ID 見 runtime 證據，官方基底 digest 未變。
- 04:04–04:07 UTC 完成全部驗收；先前停機原因調查另見本文末段，直接原因仍未確定。

| 驗證 | 結果／證據 |
|---|---|
| 基線單元／契約 | `uv lock --check --offline`、27 項 pytest 通過 |
| 一般／SSE、輸入／認證／接收上限 | 通過，[API 驗收](../evidence/2026-09-16/acceptance.json) |
| 超載、deadline、取消、API／worker 重啟 | 6×429 + 2×504；保留未完成 lease、完成後歸零；API 重啟隔離未決工作；worker 不可用回 503，新 epoch 暖機後回 200 |
| 圖片與工具往返 | 合成紅色圖片辨識、工具參數及結果 731 通過，[多模態驗收](../evidence/2026-09-16/multimodal.json) |
| SSE 已回 200 後的逾時／斷線 | error frame／無 DONE、保留工作到 terminal 後歸零，[串流生命週期](../evidence/2026-09-16/stream-lifecycle.json) |
| 版本與來源 | API 原始碼／鎖檔相符；ready=true、inflight/detached/uncertain=0，[Runtime](../evidence/2026-09-16/runtime.json) |
| 驗收後停機 | API／worker exit 0、未 paused、port 18080 關閉；模型 inventory 未變、state 保留；其他服務未重啟，[最終狀態](../evidence/2026-09-16/final-state.json) |

本次使用以下命令（先盤點共用資源；服務啟動後須完成整段並停機）：

```bash
uv run --locked modelctl doctor
uv run --locked modelctl inspect Qwen/Qwen3.5-4B
uv run --locked modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
uv run --locked python scripts/acceptance.py --faults --output evidence/2026-09-16/acceptance.json
uv run --locked python scripts/multimodal_smoke.py --output evidence/2026-09-16/multimodal.json
uv run --locked python scripts/stream_lifecycle_check.py --output evidence/2026-09-16/stream-lifecycle.json
uv run --locked python scripts/capture_runtime.py --output evidence/2026-09-16/runtime.json
uv run --locked modelctl stop
```

原始啟動前／停機後 inspect 與 logs 保存在 `evidence/raw/2026-09-16-before-start/`、`evidence/raw/2026-09-16-after-stop/`，不提交 Git；摘要保留雜湊供本機核對。這次仍不代表 Linux 實體桌機、外部 HF 下載或產品級 agent 品質已驗證。

## 契約與單元測試（不需 GPU）

```bash
uv run --locked pytest -q
```

包含 registry 不改動權重、固定 revision／混合 revision 拒絕、缺 shard、檔案變更、錯誤欄位、API 驗證、deadline／斷線 lease、API 重啟隔離、SSE terminal／截斷／慢速消費者、inline image 與 tool history。

## 真實 GPU 服務

1. `uv run --locked modelctl doctor` 唯讀盤點 host 與共用資源。
2. register／fetch 選定模型，再 serve。不要停止其他專案服務。
3. 輪詢 `/health/ready` 到 200。
4. 執行：

```bash
uv run --locked python scripts/acceptance.py --faults --output evidence/acceptance.json
uv run --locked python scripts/multimodal_smoke.py --output evidence/multimodal.json
uv run --locked python scripts/stream_lifecycle_check.py
uv run --locked python scripts/capture_runtime.py
```

使用合成、非敏感文字；驗證模型列表、一般／SSE、錯誤參數、未知模型、context、認證、body 上限。`--faults` 只對經 Compose label 核對的 selfhost-models worker 做 pause/unpause，確認 429、deadline 504、斷線 lease 不提前釋放；再重啟 API 驗證未決工作保留，重啟 worker 驗證新 epoch 暖機恢復。finally 會 unpause worker，不刻意耗盡 GPU。

四個腳本皆可使用 `--url`、`--state` 與 `--output <path>`，複驗時使用新的日期目錄保留舊證據。`capture_runtime.py` 保留容器內 API 原始碼／鎖檔與 checkout 的原始 SHA256，僅容許 CRLF／LF 轉換且明列差異；其他來源差異仍非零退出，不能拿舊 image 的結果代表新版本。

測試成功只表示服務協定與受控生命週期成功，不是產品效能、agent 成功率或領域模型品質。若任何 assertion 失敗，命令非零退出，不得把部分結果當全部通過。

## 主機差異

相同 Python／Compose 命令可在 Linux 重跑，但需先配置 NVIDIA Container Toolkit 及 Linux 的模型 path。Windows WSL2 成功不表示 Linux 桌機已驗證。以每次 JSON evidence 的時間、model revision、host/runtime 紀錄識別結果。

## 首次 GPU 驗收紀錄（2026-09-15）

本次驗收模型為 `Qwen/Qwen3.5-4B`，固定 revision `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`；唯讀接入 `D:/hf_models/Qwen3.5-4B`。Host 為 Windows Docker Desktop WSL2、RTX 5090 Laptop 24463 MiB、driver 581.57。

設定：BF16、context 2048、最多 2 個工作、等待佇列 0、GPU memory utilization 0.60、client deadline 30 秒、drain 上限 120 秒。可重現的啟動命令：

```bash
uv run --locked modelctl serve Qwen/Qwen3.5-4B --gpu-memory 0.60 --context 2048 --max-inflight 2
```

本次 JSON 證據：

- [API 與受控故障](../evidence/acceptance.json)：2026-09-15 09:27–09:29 UTC。
- [合成圖片與工具往返](../evidence/multimodal.json)：09:30 UTC。
- [SSE deadline／disconnect](../evidence/stream-lifecycle.json)：09:30 UTC。
- [Runtime、隔離與最終 readiness](../evidence/runtime.json)：包含 image ID、來源檔 SHA256、GPU 與固定模型 revision。`git_head` 是擷取當下的前一提交；`git_status` 明示尚未提交的驗收修正，來源 hash 用於核對本次實際程式。
- [選模前的歷史檢查](../evidence/pre-model-validation.json)：保留初期狀態，不代表最新驗收結果。

| 層級 | 2026-09-15 結果 |
|---|---|
| 單元／契約 | 27 passed |
| 實際 API container | loopback 連入、401、worker 缺席 503、持久化 fsync 通過；測試容器已移除 |
| 官方 worker runtime CUDA | 0.29.0 / torch 2.13.0+cu130 / CUDA 13.0 / transformers 5.16.1；八元素 tensor 計算通過 |
| 既有模型 register／inspect | Qwen3.5-4B 一致 revision、11 個必要檔案，未改動權重 |
| 真實模型 GPU 推論／完整故障驗收 | 通過一般／SSE、400／401／404／413、受控超載 6×429 + 2×504；保留 2 個未完成 lease，恢復後歸零 |
| 取消、API／worker 重啟 | 斷線保留 lease；API 重啟後未決工作使 ready=false；worker 不可用回 503；新 epoch 暖機後恢復，所有 lease 歸零 |
| 已送出 SSE 200 後的 deadline／disconnect | 通過；deadline 回 error frame 且不送 DONE；兩者都持續追蹤 GPU 工作到完成 |
| 真實 visual／tool roundtrip | 合成紅色 PNG 辨識為 Red；工具參數合法、由測試 client 執行合成查詢，模型接收結果並回答 731 |
| Linux 實體桌機 | **尚未驗證** |

實測修正：WSL2 的 V2 runner 因 UVA 不可用，明確改用同一官方 runtime 的 V1 runner。單 token 暖機未涵蓋 decode 編譯，首個正式請求曾逾時；暖機改為多 token、併發及合成視覺後，重新啟動並完成上述整套驗收。首次或新輸入 shape 的編譯時間仍可能不同。

最終快照 ready=true、inflight/detached/uncertain 皆 0；整張 GPU 使用 18805 MiB、空閒 5247 MiB。這包含桌面與 vLLM 常駐權重／cache，不是純權重大小。請求完成會釋放 admission 名額；`uv run --locked modelctl stop` 才停止本專案容器並歸還其常駐 GPU 記憶體。

fetch 的固定 revision 行為有契約測試，尚未實際下載外部模型。工具 smoke 不代表長程 agent 成功率；本版不提供音訊／影片 API、任意 URL 圖片或產品工具執行。

## uv 遷移驗證（2026-09-16）

此輪只變更 Python 套件管理與 API image 的安裝方式；Docker／Compose 管理容器，vLLM worker 的 CUDA／PyTorch 配套未變。上方 2026-09-15 GPU 證據保留原樣，本輪未啟動 GPU worker 或重跑 GPU 驗收。

uv 0.12.15 + Python 3.12.14：舊 `requirements.lock` 的 30 個套件版本與 `uv.lock` 逐一比對無差異。PATH 既有 uv 0.11.21 也通過同一鎖檔的離線 sync、27 項測試與 CLI 檢查；因此專案支援 >=0.11.21,<0.13，Docker 工具固定 0.12.15。Windows 安裝依上游 metadata 排除 hf-xet，Linux API image 安裝 hf-xet 1.6.0。

| 指令／檢查 | 結果 |
|---|---|
| `uv sync --locked` | 新建 `.venv` 並安裝成功 |
| `uv lock --check --offline` | 鎖檔與專案一致 |
| `uv run --locked pytest -q` | 27 passed |
| `uv pip check` | 30 個已安裝套件相容 |
| `uv run --locked modelctl --help` | CLI entry point 正常 |
| `docker build -f docker/api.Dockerfile -t selfhost-models-api:0.1.0 .` | 成功，兩段 `uv sync --locked --no-dev` |
| `uv run --locked python scripts/api_container_check.py` | loopback、401、worker 缺席 503、持久化 fsync 通過；測試容器已移除 |
| 新 image 的 `--network none --read-only` Python import | 成功，`sys.prefix=/app/.venv`，pytest 不存在 |

新 API image ID（`docker image inspect`）：`sha256:62f1c05a6bdcfcd37677f43f422d8134e596364b863bfeba212fb94cad73d22b`。此 Linux container 建置／執行證據來自 Windows Docker Desktop，不等於 Linux 實體桌機驗證。

## Exited (255) 調查（2026-09-16，啟動前）

已先保存兩個舊容器的完整 inspect／logs，再執行任何啟動或重建。可提交的 [調查摘要與原始檔 SHA256](../evidence/2026-09-16/exit-investigation.json) 指向本機 `evidence/raw/2026-09-16-before-start/`；原始 inspect、完整日誌及主機事件留在 Git 忽略目錄，避免將主機細節混入公開成果。

已確認：

- API／worker 均為 exit 255、`OOMKilled=false`、`Error` 空字串、restart policy `no`；啟動前工作 lease 為空。
- Windows 系統事件 1074、13、12 記錄 9/15 18:29–18:31（台北時間）由系統更新觸發的計畫重啟；API 最後健康日誌為 18:29:17。
- Docker Desktop 在 9/16 09:59 啟動 daemon、載入容器；兩個舊容器 `FinishedAt` 同為 09:59:19，該時段還有它們的 `layer not mounted` 與 `Removing stale sandbox` 紀錄。
- Docker event 查詢未保留對應的原始退出事件；應用程式日誌没有該結束時間的直接退出原因。

**255 的直接原因仍無法確定。** 上述時序不能證明是應用程式崩潰、CUDA OOM 或特定 Docker bug，也不能將 daemon 恢復時寫下的時間直接當成實際程序終止時間。本次不據此更換 runtime 或加入自動重啟政策。
