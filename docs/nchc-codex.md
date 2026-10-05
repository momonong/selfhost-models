# 在研究專案使用國網：給 Codex 的參考指南

在**研究 repo** 開 Codex，貼下方指示詞即可先準備工作。研究程式、設定、Slurm 腳本與 run receipts 留在研究 repo；`selfhost-models` 是唯讀參考，不需要為此安裝它的 Python package、Docker 或 UI。讀取本文件不授權登入、傳輸、安裝遠端環境或使用額度。

## 可直接貼到研究 repo 的指示詞

```text
請先讀本研究 repo 的 AGENTS.md、README 與訓練入口。定位 selfhost-models：先用我指定或已登錄的位置，再查 PROJECTS_ROOT；未設定時只檢查原生 home 下的 projects/selfhost-models 候選。核對 git remote 是 momonong/selfhost-models、docs/nchc-codex.md 存在，記錄 HEAD；指定位置無效就回報，找不到才問，不掃全磁碟或自行 clone。唯讀參考 docs/nchc-codex.md 與 examples/nchc-smoke/README.md，不將參考 repo 的 AGENTS 套用到本研究 repo。按指南先盤點模型、locked 環境、entry/args、資料/checkpoint、資源及輸出需求，缺必要資訊一次彙整問；在本研究 repo 準備可審查的版本快照方案、傳輸 manifest、Slurm 腳本、分段命令與驗收條件。先完成離線準備；沒有另外授權就不要連線國網、提交工作、花額度或改本參考 repo。
```

已安裝 repo 的定位以實際 OS 與主機為準；不要猜 Windows 磁碟、macOS／Linux 使用者名稱，也不要把本機路徑當遠端路徑。核對 `git remote -v`、`git rev-parse HEAD` 與文件內容即可，remote 若嵌有憑證只記安全的 owner/repo。參考文件版本須與已讀 HEAD 一起寫入研究 run 計畫，不需使用者手填 commit。

## 目前成果與證據

| 成果 | 能直接使用什麼 | 證據與限制 |
| --- | --- | --- |
| [個人操作筆記](nchc-personal.md) | 終端位置、SSH／SFTP、手動 Slurm 流程 | 個人帳號／account 是歷史設定，不作通用模板預設；每次重新核對 |
| [固定 55 樣本](../examples/nchc-smoke/README.md) | 理解腳本、輸入、提交、step 與下載的關係 | `make_sample.py` 只生成標準函式庫固定樣本，不生成訓練工作 |
| Nano5 手動流程 | 2026-10-03 Job 369175：CPU 加總 55、主 job 與 steps 為 `COMPLETED 0:0` | 申請 GPU 但 `gpu_computation=false`；[65-byte 結果](../evidence/2026-10-03-nano5-smoke/nano5-369175-result.json) SHA256 為 `566774a5a7ebfeb173ee1401abdc257bdcea403bb0613fb7e0ac3d1d5722afa7`；遠端狀態由使用者回報，下載檔本機核對 |
| `nano5-batch` | 保留的離線 UI／工作快照與手動結果保存 | 真實國網連線／submit 硬性停用，UI 停止擴充；[v2 工程證據](../evidence/2026-10-02-nano5-v2/README.md) 不等於真機驗收 |
| `modelctl`／模型 API | 本專案既有推論基礎設施 | 不是 Slurm 訓練提交器，不將推論 Docker／CUDA 配套搬去作研究環境 |
| 本指南與 rc3 | 外部 Codex 可據文件準備研究工作 | 未驗 GPU 訓練、研究依賴、SIF、torchrun／多節點、大型資料搬運或所有 OS 實機 |

## 先在研究 repo 完成離線準備

先讀研究 repo 自身規則及已存在的訓練／部署文件，不從此參考 repo 改變研究假設、評估標準或依賴管理方式。盤點以下必要資料，缺項一次彙整給使用者：

| 輸入 | 要確認的內容 |
| --- | --- |
| 程式與模型 | 訓練 entry、完整 args／config、模型來源與固定 revision、license、是否會自動下載 |
| 環境 | Python、框架／CUDA 版本、locked dependency manifest、module／conda／SIF 選擇及可取得方式 |
| 資產 | dataset／checkpoint 的本機與遠端位置、檔案數／bytes、存取限制、既有資產能否重用 |
| 資源 | 單／多節點需求、GPU 數與最低 VRAM、CPU／RAM、walltime、容量、smoke 的步數／資料上限 |
| 成果 | output／checkpoint／log 位置、恢復方式、完成與品質條件、下載到研究 repo 的哪個目錄 |
| 真機階段 | 人類在場時間、有效 account／partition、可用額度、費用上限、停止／取消責任 |

在研究 repo 保存一份可審查計畫（例如 `runs/<run-id>/plan.md`）及必要檔案，名稱沿用該 repo 慣例。至少交付：code snapshot／manifest、環境 manifest、smoke 與完整訓練的 job 腳本草稿、傳輸清單、逐段命令與驗收。文件中所有 `<...>` 或 `__...__` 都是**未填欄位**，不可原封執行或提交；Codex 必須產生已替換、可審查的實際版本。

### 固定程式與資產

- 記錄研究 repo HEAD、branch、dirty／untracked 狀態。選擇已確認的乾淨 commit，或明列本次納入的 tracked 修改與必要 untracked 檔；不要用 `git archive HEAD` 默默漏掉工作樹變更。已提交且不需未提交內容時才用 archive。
- 以明確 allowlist 打包必要 code、config、job、鎖檔／依賴 manifest，記每檔相對路徑、bytes、SHA256 與整包 hash。不要打包整個 home、`.git`、venv、秘密、憑證或受限原始資料，不擅自 commit／公開研究資料。固定快照之後的新修改另記版本，不能靜默換程式。
- 大型 dataset、模型權重與 checkpoint 和 code 分開。傳輸清單記來源／目的地、大小、hash、授權及遠端既有副本；先核對 quota／可用容量，再規劃重用或有界傳輸。不把大型資產塞進離線 UI 的 16 MiB／100 檔快照。
- 每次 run 使用新的遠端工作目錄；續訓顯式引用已確認 checkpoint 與 hash，輸出另存，不覆寫舊結果。遠端 `/work` 保留期限／配額由當次官方規則與帳號狀態確認；它不是永久備份。

### 研究環境與 job 腳本

`/usr/bin/python3` 的 55 樣本成功，只證明該次標準函式庫程式可用，不能推論 torch 或 CUDA 已就緒。依研究 locked manifest 選環境；[官方環境範例](https://man.twcc.ai/@AI-Pilot/SyXURRpP1x) 是操作參考，不是所有研究的固定 PyTorch／CUDA 配方。不自動升級或混用系統 Python、conda、SIF 與本 repo 的推論 runtime。

job 草稿需明列：account、partition、nodes／tasks、每 task CPU、GPU／VRAM需求、RAM、walltime、工作目錄、`.out`／`.err`、環境初始化、entry／args、輸入與輸出。`#SBATCH` 中 `$變數` 不會由 shell 展開，路徑與資源必須在生成腳本時填成已核對的字面值；Slurm 只傳 batch 腳本，不替你搬 code／資料。[sbatch 契約](https://slurm.schedmd.com/sbatch.html)

環境核對分兩處：登入節點只做已授權的輕量版本／路徑／module 查詢；計算節點在短 job 中記 Python executable/version、GPU 型號／driver、框架版本與 CUDA runtime，並確認實際 GPU forward/backward 及產物。若使用 SIF，另核對容器執行工具、SIF hash、bind 路徑、GPU 參數與相容性；本 repo 沒有已驗證的 SIF。`torchrun`／MPI／多節點需研究需求、當地規則與獨立 smoke，不能視為已支援。

腳本遵循研究 repo 規則，保存失敗 exit code，不以 CPU fallback 假裝 GPU 成功。smoke 預設不自動重試；可選 `--no-requeue` 並記錄，但叢集管理設定可能覆寫，仍須觀察實際紀錄。勿在 login node 執行訓練、重型預處理或服務；也不要把 job 腳本以 `bash job.slurm` 直接執行。

## 分段真機驗收與授權

| 階段 | 批准前可交付的內容 | 通過／停止條件 |
| --- | --- | --- |
| A．離線準備 | 上述計畫、manifest、腳本、命令與驗收 | 缺必要資料或仍有 placeholder 就停，先完成可審查成果 |
| B．有界唯讀 preflight | 列明登入端、查詢項目、最多一次查詢及需要的人類認證 | 人類在場輸入密碼／OTP；核對機器、account／partition 權限、wallet 有效期／正餘額、環境及容量；不足就停 |
| C．一個短 GPU／研究 smoke | 明確 GPU／CPU／RAM／walltime、步數／資料量、額度或費用上限、一個 job 與驗收產物 | 已有明確資源、費用與操作授權時直接提交一次，不重問；未授權或超出範圍才另問。實際 GPU 計算、job＋steps、exit code、checkpoint／產物都通過才可進下一段 |
| D．完整訓練 | 基於 smoke 的完整資源、費用上限、checkpoint、下載及停止計畫 | 另獲授權或既有明確階段授權後執行；工程成功與研究品質分開驗收 |

讀指南、git pull、已驗 CPU55 或 B 通過，都不自動授權 C／D。使用者已授權範圍內的低風險例行操作直接做，不每條命令重問；未授權花費先完成可審查的計畫再問。Slurm 分配節點，不手挑 `hgpn`；55 的 dev／1 GPU／兩分鐘是歷史樣本，**不是訓練預設**。

## 各執行端的命令

以下是後續已授權階段的命令參考，本輪文件維護不執行。Codex 先將未填欄位換成計畫中的實值並檢查；本機、遠端與 `sftp>` 是三種不同執行端。

### 本機終端：登入與傳輸

Ubuntu／macOS／Windows 具備 OpenSSH `ssh`／`sftp` 時，可使用相同單行命令；原生 home、本機目錄切換與檔案 hash 則依各 OS／shell API 處理。Windows 不照貼後面的 Bash `read`／`[[ ]]`；不要要求每台電腦安裝 uv 或本 package。

```text
ssh -p 22 __NANO5_USER__@nano5.nchc.org.tw
sftp -P 2222 __NANO5_USER__@nano5.nchc.org.tw
```

`__NANO5_USER__` 必填當次確認的帳號，不能拿個人歷史帳號當其他人的預設。密碼／OTP 僅由人類輸入終端認證提示，工具不得保存或自動讀取。主機指紋未確認先透過可信來源核對，不盲目接受；登入失敗不改用猜測主機。SSH 22 與傳輸 2222 的用途不同。[登入規則](https://man.twcc.ai/@AI-Pilot/SkxWj5GwY1g)

### Nano5 遠端 Bash：唯讀 preflight

僅在 B 已授權後，按計畫查一次。下面各命令由 Codex 生成實值版本；`wallet` 輸出可含餘額，保留在適當私有位置，公開收據只記核對結果，不貼完整輸出。

```text
wallet __ACCOUNT__
sinfo -s
scontrol show partition __PARTITION__
command -v python3
python3 --version
ml list
df -h __REMOTE_WORK_ROOT__
```

`sinfo` 顯示存在不代表你的 account 可提交；須核對帳號關聯／partition 權限與當次官方限額，不猜 account 或自動選第一個計畫。`python3` 是初步系統資訊，研究環境還需按上節核對；某命令不存在就記缺口，不自動安裝、換環境或試遍所有工具。容量檢查不是個人 quota 保證，另查當地 quota；GPU 實算留在 C。

### Nano5 遠端 Bash：審查與一次提交

先 `bash -n` 與閱讀已填值的腳本；此處只審查，不提交。括號是 subshell，錯誤不關閉原 SSH 視窗。這項 placeholder 檢查只是最後一道檢查，不能取代資源／費用／環境的逐項審查。

```bash
(
  read -r -p '已準備且已填值的 /work job 腳本完整路徑：' NCHC_JOB_SCRIPT
  [[ "$NCHC_JOB_SCRIPT" == /work/* && -f "$NCHC_JOB_SCRIPT" ]] || { echo '停止：腳本不存在。' >&2; exit 1; }
  if LC_ALL=C grep -Eq '__[A-Z][A-Z0-9_]*__|<[^>]+>|TODO|CHANGE_ME' "$NCHC_JOB_SCRIPT"; then
    echo '停止：仍有未填欄位，請人工核對。' >&2
    exit 1
  fi
  bash -n "$NCHC_JOB_SCRIPT" || exit 1
  cat -- "$NCHC_JOB_SCRIPT"
)
```

完成 C 或 D 的明確批准後，Codex 再提供計畫中的實值提交命令，**只執行一次**：

```text
sbatch __ABSOLUTE_REVIEWED_JOB_SCRIPT__
```

保存提交 stdout／stderr、時間與回覆 Job ID。成功取得 ID 只是已提交，可能仍排隊。[官方工作管理](https://man.twcc.ai/@AI-Pilot/r1os5G_Mkl) 提供 sbatch／srun／salloc 用法，本指南以可追蹤 batch job 為主，不另開無上限 interactive allocation。

### Nano5 遠端 Bash：狀態與收據

一次讀取已知 Job ID，保留主 job、batch、extern 及計算 steps，不用 `sacct -X` 省略 steps。以下輸入拒絕非數字欄位：

```bash
(
  read -r -p 'sbatch 回覆的數字 Job ID：' NCHC_JOB_ID
  [[ "$NCHC_JOB_ID" =~ ^[0-9]+$ ]] || { echo '停止：Job ID 必須是數字。' >&2; exit 1; }
  sacct -j "$NCHC_JOB_ID" --parsable2 --format=JobIDRaw,JobName,Account,Partition,State,ExitCode,Start,End,Elapsed
)
```

未完成時稍後手動查一次；若需看排隊原因，由 Codex 提供填值的 `squeue -j __JOB_ID__`。不使用 watch、迴圈或自動重試平台。欄位若與當地 Slurm 版本不相容，保存錯誤與版本，依官方 `sacct --helpformat` 調整，不把缺資料當成功。[sacct 欄位](https://slurm.schedmd.com/sacct.html)

| 觀察 | 判斷／動作 |
| --- | --- |
| PENDING／RUNNING／COMPLETING | 排隊、執行或清理中，尚未成功完成 |
| 主 job 與相關 steps 均 COMPLETED／0:0 | 再核對 stderr、GPU 證據、checkpoint／產物完整性與研究驗收 |
| FAILED／TIMEOUT／OUT_OF_MEMORY／NODE_FAIL／CANCELLED | 失敗或停止，保存日誌／checkpoint，不自動重送 |
| squeue 沒顯示／sacct 空白／提交回覆遺失 | 明確記 unknown 或觀察缺口；不由「消失」推論完成 |

若 `sbatch` 回覆不明，先保留時間、job-name、腳本 hash 與回覆，於已授權範圍核對當次帳號紀錄；不能再送一次來確認。取消需原階段授權，以已確認 ID 執行一次 `scancel` 並再觀察終態；取消請求、斷線或程序消失不等於已完成清理或沒有費用。[Slurm 狀態](https://slurm.schedmd.com/job_state_codes.html)

### sftp 提示端：下載到研究 repo

登入傳輸節點後先 `lpwd`／`pwd` 核對端點。Codex 按計畫提供下列填值命令，不把其他 repo 或任意目前目錄當預設下載位置；先確認目標新目錄已存在且不會覆寫，hash 比對用當地 OS 工具。

```text
lcd "__LOCAL_RESEARCH_RUN_DOWNLOAD_DIR__"
cd "__REMOTE_RUN_OUTPUT_DIR__"
lpwd
pwd
get __ARTIFACT_FILENAME__
bye
```

逐項核對預期 bytes／SHA256，保存 `.out`、`.err`、環境紀錄、checkpoint 與研究產物；不要只下載最後 JSON。遠端產物 manifest／hash 應由 job 在已分配資源內產生；不要為大型 checkpoint 在登入節點做重型掃描。大型傳輸另有清單與容量／時間上限，不以這份小檔命令宣稱已驗大資料傳輸。[官方傳輸說明](https://man.twcc.ai/@AI-Pilot/SkDyJN4Gkl)

研究 run receipt 至少記：參考 repo HEAD、研究 code commit／dirty snapshot hash、環境／鎖檔／SIF hash、entry／args／seed、必要非秘密 account、資源／費用上限、遠端工作目錄、提交回覆與 Job ID、start／end／state／ExitCode（主 job＋steps）、產物清單／bytes／hash／本機目的地、最後成功階段與未驗事項。缺值明列 unknown；不保存密碼／OTP、完整登入輸出、token 或私密 wallet 餘額到公開 Git。是否提交研究收據依研究 repo 的資料政策與授權。

## 實驗室電腦更新此參考 repo

此段在**本機 selfhost-models 目錄**執行，各命令為獨立單行；PowerShell 也可用 Git 單行命令，目錄定位另用原生方式。

```text
git remote -v
git status --short --branch
git branch --show-current
git rev-parse --abbrev-ref --symbolic-full-name "@{u}"
```

確認正確 repo、工作目錄乾淨、branch=`main`、upstream=`origin/main`，並在獲准更新後才執行：

```text
git pull --ff-only
git rev-parse HEAD
git status --short --branch
```

dirty、upstream 錯誤或分歧就停，保留現況；不要 reset、stash、覆寫或強行切分支。成功後檢查 `pyproject.toml`、`selfhost_models/__init__.py`、`uv.lock` 均為 `0.3.0rc3`（若後續版本已發布，依其版本紀錄核對），`docs/nchc-codex.md` 存在，記實際 HEAD 與版本。pull 更新來源，不替你安裝 package、更新 images 或部署服務；離線參考也不需這些操作。

## 官方查核與當次限制

2026-10-05（臺灣）僅公開 web 查閱，未做國網 DNS probe／SSH／SFTP／submit。以下頁面可讀，連結支援上面的 port、查詢、環境及 Slurm 命令契約；頁面範例不代表此帳號已驗收。

- [Nano5 登入與使用注意事項](https://man.twcc.ai/@AI-Pilot/SkxWj5GwY1g)：SSH 22／傳輸 2222、登入節點限制、不頻繁輪詢；境外連線需另查申請條件。
- [Nano5 partition 清單](https://man.twcc.ai/@AI-Pilot/BJYB5G_zke)：頁面表格更新日 2026-07-01，列 dev、normal、normal2、4nodes 及不同時間／GPU 限額；不能由這份公開表推論帳號權限，當次重查。
- [wallet](https://man.twcc.ai/@AI-Pilot/rygXKNuNMyg)：有效計畫與正餘額是派送前提。費率、額度、有效期不硬編，當次查核與批准上限。
- [研究環境範例](https://man.twcc.ai/@AI-Pilot/SyXURRpP1x)：module／conda 設定與計算環境須一致；版本、GPU 實算及 SIF 仍要按研究驗證。
- [工作管理](https://man.twcc.ai/@AI-Pilot/r1os5G_Mkl)、[傳輸](https://man.twcc.ai/@AI-Pilot/SkDyJN4Gkl)、[sbatch](https://slurm.schedmd.com/sbatch.html)、[sacct](https://slurm.schedmd.com/sacct.html)、[狀態碼](https://slurm.schedmd.com/job_state_codes.html)：填值、一次提交、steps 與下載位置的參考。

未來頁面不可讀或權限／環境資訊缺失，明列缺口並暫停依賴它的真機階段，不猜值。需要使用者一步步操作時，Codex 按已確認計畫一次給一段，核對該段結果再往下；本指南不新增自動登入、訓練 CLI／API 或重試服務。
