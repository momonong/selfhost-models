# Docker daemon 500：診斷與最小恢復流程

2026-09-21 更新：9/20 的唯一 Desktop restart 已成功；續作時 Desktop 已停止，
後續單次正常 start 未取得完成結果，已記為 UNKNOWN／STOP。不得重用下列歷史授權
自行重送 lifecycle。最新證據見 [啟動與控制程式收斂](../evidence/2026-09-21-desktop-start-stop/README.md)。

**以下為 9/20 執行前保存的恢復規格；不是目前狀態或可重用的 lifecycle 授權。**
main 統一協調跨專案。daemon 恢復、原 Qwen 恢復、D 驗收分別保存證據；只有本流程
成功、身份及資源核對通過後，才能依[已授權 D 窗口](scheduler-gpu-acceptance.md)繼續
候選建置與切換。恢復失敗後不得繼續其他 Docker 操作。

## 已知時間線（2026-09-20 UTC）

| 時間 | 已知事實 |
|---|---|
| 05:22:58 起 | 初始唯讀 ready=true、lease=0、固定Qwen；記錄13個當時running容器及兩個selfhost的完整image/config身份 |
| 06:25:36.710873 | 最後成功 Whisper CPU refresh build log 寫入，image `6efb33d19194…` |
| 06:29:54.903275 | 本輪CPU relay fixture兩容器、兩network清理receipt完成，12個核對/stop/rm指令exit0；未操作volume |
| 06:30:33.300360 | 固定vLLM base的local image inspect成功；Qwen候選尚未build |
| 06:30:33–06:31:49 | `docker buildx history ls --format json` 首見RPC EOF，接續image ls的ping回500；沒有保存精確首錯秒數 |
| 隨後單次重查 | `docker version --format '{{json .}}'` 回Server=null／500 |
| 06:33:58 之前 | 現役18080 ready 5秒逾時，故未query models；GPU used2256MiB/util0%，不構成engine退出證據 |

完整命令、fixture IDs/labels、exit codes、raw receipt hashes見
[incident.json](../evidence/2026-09-20-scheduler-cpu/incident.json)。原始stdout/inspect保留
ignored `.state/scheduler-asr-preparation`，未公開主機完整盤點、秘密或私人payload。
Qwen build/create/run均未送出，只有固定base讀取與source snapshot準備。

根因**未知**。CPU build、fixture清理與故障的時間先後，不足以認定或排除因果。
main另回報14:34台北backend程序仍是9/16啟動、UI子程序14:32:18啟動，以及球館同時
遇到500；這些是協調回報，不是本task重新量測或根因證明。舊owner聲明本窗口沒有
維護動作，也不能代替daemon／host根因診斷。停止反覆probe及新增Docker寫入。

## 需保護的最後已知狀態

完整13個container的ID/name/image ref/CreatedAt/Compose project/service清單留於
ignored `.state/scheduler-incident/recovery-baseline.json`。包含selfhost2個、KaChing4個、
府城7個；不能把初始清單視為事故前最後一秒或目前狀態。其他專案若在這段時間有合法
變更，須由其owner提供更新的比對基線，不把差異自動認成遺失或錯誤。

原selfhost身份：

- API：`selfhost-models-api-1`，container
  `f35e32eed86cdbbfd436949f9d4b13e2e6d79eeca0ed5dbb769505ac0a0251b2`，image
  `sha256:04c47cc62471c2a101bda23f911f0f00a78b6081803cadee09b4c98e2888101f`。
- Worker：`selfhost-models-worker-1`，container
  `5036e914ce6db9007027178596015724c5d13837f16924cdbadeb076e540fbc9`，image
  `sha256:d209ab188d96f9bce180cf59fffefb3fe021de71e34c85dd8d0893800736491c`。
- Compose project `selfhost-models`，API只掛原key唯讀與`.state/runtime`讀寫，worker只
  掛原固定Qwen目錄唯讀。原inference/ingress networks、loopback18080、revision
  `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`、context8192/capacity2/video/gpu0.60。
  model路徑與完整非secret env/config在private baseline；API key內容不記錄。

事故後僅對既有Compose設定、compose.env及journal檔做本機完整SHA256基線；不是
事故前hash，也不是其他專案資料備份。不得刪state、journal、volume、container、
image或weights，不重建任何原容器。

## 建議只做一次的 Docker Desktop 恢復

前提：orchestrate 傳回 main 協調後的 fresh writers／RESOURCE GO 與維護截止時刻。
正常app啟動／自動backup仍可能寫入，不以主動使用者釋放推定所有writer已靜止。
本機`docker desktop restart --help`已證實支援`--timeout seconds`，本次help命令exit0。

整個流程由同一個執行者維持單一monotonic deadline：第一個恢復命令之前設定
`deadline = time.monotonic() + 480`。每個子程序以固定argv、`shell=False`執行，
`timeout=min(該步上限, deadline-time.monotonic())`；剩餘時間≤0時不得送命令。
Desktop restart的CLI `--timeout` 也不得超過剩餘總額度，外層子程序同樣限時，不能
只依Docker內建timeout。階段等待及sleep亦計入這480秒，不在每次操作重置deadline。

Windows 控制程式使用 [run_bounded](../scripts/bounded_command.py)：stdout/stderr 寫入
新的私有檔案，使用 `Popen.wait`，不可用 `capture_output=True`／PIPE加上超時後的
無界 `communicate()`。子孫持有輸出handle不應讓已退出CLI的等待持續。單次上限包含
client清理，預留最多2秒給kill/wait；只操作該次Popen process handle，不殺process tree、
Desktop/backend、WSL或其他程序。timeout即外部結果unknown，清理失敗同樣STOP。
OS層的CreateProcess/kill若本身停滯仍不能承諾實時硬界限，須如實回報。

global `deadline` 在整個恢復流程只建一次，不能每次呼叫重算480秒；`timeout_seconds`
另傳單步180/10/30等上限。輸出檔可能仍由存活子孫寫入，hash應標為讀取時間的快照。
file-backed capture前就以CLI格式選取所需欄位，不能先保存完整inspect再刪去Env／secret。

| 操作 | 單次子程序上限 |
|---|---:|
| `docker desktop restart` | 180秒，且不超過總剩餘時間 |
| `docker version`、`ps`、各次 `inspect` | 各10秒，且不超過總剩餘時間 |
| 各個原容器 `docker start` | 各30秒，且不超過總剩餘時間 |
| HTTP ready/models | 各5秒，且不超過總剩餘時間 |

**任何子程序timeout、結果遺失或不確定，立即停止下一步並回報。**外層timeout終止
Docker CLI client，不代表daemon沒有執行命令；尤其start不能重送，不能推定container
沒啟動或已停止。已完成的唯讀probe明確回Server=null可依下方有界次數等待；probe若
逾時或結果未知則不再讀。只保存所需identity／mount／health欄位，禁止完整Env或key
內容流入receipt；其他app資料payload不讀取或保存。

```powershell
docker desktop restart --timeout 180
```

只執行一次，保存開始/結束時間、exit code與有限stdout/stderr。**nonzero或180秒到期
立即停止並回main，不再進行下方健康讀取或任何恢復步驟。**這不表示後端沒有繼續啟動；
不得下第二個restart、stop/start、殺backend、更新Docker或`wsl --shutdown`。

只有restart命令成功後，才於0／30／60秒，最多3次`docker version --format '{{json .Server}}'`，每次
由呼叫端限時10秒；只有Server非null才查一次`docker ps -a`及待核對的原容器inspect。
三次皆不可用即停止回main，不自行升級到WSL/host層恢復。KaChing四服務的
`restart: unless-stopped`已由main核對；daemon恢復可能自動啟動它們並發生正常startup
資料寫入。其writer狀態與球館automatic backup寫入狀態皆UNKNOWN，不能承諾其他app
不啟動或零寫入。不改restart policy，也不阻止或重建其他app。

## 恢復後核對與原 Qwen 啟動

1. 比對原container ID、Created、image ID/ref、Compose labels、mount來源與RW旗標、
   ports、networks。名稱相同而ID不同代表可能recreate，須停下交owner查證；network
   endpoint/IP在daemon恢復後可變，不把它單獨當作資料損毀。
2. 容器未recreate不代表資料沒改動。只核對selfhost原config/journal結構與hash，禁止
   寫入或清除未知lease；其他DB/backup/migration／檔案完整性由各app owner按其基線
   查驗。本task不替其他專案啟停、migration、backup或宣稱資料完整。
3. 原Qwen若已running，不再次restart。若原worker/API明確exited、精確身份一致，且
   主動恢復原Qwen已獲核定，只start當前未running的原容器，順序worker→API：

   ```powershell
   docker start 5036e914ce6db9007027178596015724c5d13837f16924cdbadeb076e540fbc9
   docker start f35e32eed86cdbbfd436949f9d4b13e2e6d79eeca0ed5dbb769505ac0a0251b2
   ```

   每個最多一次；running者跳過。不使用`compose up`、build、pull、recreate，不建立
   managed state／ownership gate或啟動新候選。若原container不見或不是exited/running，
   停止交main；不按名稱重建。這段是事故修復提案，不是D切換runbook。
4. 最多180秒、每15秒讀一次ready（每次HTTP timeout5秒）；取得ready=true後才安全
   讀原key查models，核對image/revision/context/capacity/video和lease全零。原服務
   自身暖機會執行GPU；不另加人工smoke、scheduler job或推論。readiness未成功、
   unknown lease、GPU/RAM異常或identity差異即停止回報，不再restart／清state。
5. selfhost回報原服務恢復證據；main收集球館／PixelReceipt／KaChing各自驗證後判斷
   整體事故是否解除。其他業務驗收未完成時，不以selfhost ready宣稱全系統正常。

整體單次恢復流程以同一480秒monotonic deadline為硬上限；後面的daemon等待、inspect、
start與Qwen暖機都只能使用剩餘時間，因此Qwen可等的時間可能少於180秒。超時保留已知
狀態與命令結果不確定性，下一層處置重新交main。恢復可能新增的原Qwen load/warmup
也納入D全局12／64 ledger的initial 1／6 reserve；未知不退額度。D階段已授權，但只有
本流程成功且fresh資源窗口仍有效時才可繼續。
