# Nano5 工作管理（本機離線版 v2）

**先設定專案一次，再選專案建立工作。** 本版協助準備給國網 Slurm 的 job，現在只在這台電腦保存，**尚未提交或排隊**。國網登入、傳輸及提交硬性停用，沒有環境變數、CLI 或 UI 開關。

## 這四種東西有什麼不同？

| 項目 | 用途 |
|---|---|
| 專案 | 整份程式、子模組與設定檔，以及預設入口/參數；可重複使用 |
| 執行環境 | 國網預先準備的 SIF，決定 Python/CUDA/套件；本版只登錄位置，未核對可用性 |
| 資料位置 | 國網既有資料集或模型權重的位置；本版只保存參照，不搬大型檔案 |
| 工作（job） | 指定一次要執行的專案版本、參數、資料與資源；正式階段由 Slurm 分配節點 |

job 提交成功也不代表立即開始執行。正常流程是準備 → 提交 Slurm → 排隊 → 執行 → 終止 → 收集成果。本版只完成第一步，沒有真實 Slurm Job ID，不會把「已準備」顯示成「排隊中」。

## 啟動與日常操作

repo 根目錄，uv 0.11.21–0.12.x，Python 預設 3.12（支援 3.11–3.13）：

```bash
uv sync --locked
uv run --locked nano5-batch --verify-offline
uv run --locked nano5-batch --state .state/nano5-batch --port 18771
```

開啟 `http://127.0.0.1:18771`。單一 process、loopback，不需 Docker/GPU，不更動既有模型 API。不建立 autostart/對外入口；先確認埠空閒，18080 不允許作本工具埠。

1. 首頁「工作」沒有專案時，選「建立專案」。名稱可依研究或程式用途取名。
2. 在「專案」一次選取**完整程式資料夾**。瀏覽器明確選取，不由 HTTP 掃描磁碟、repo 或本機絕對路徑。保留相對階層；包含子模組與相對設定檔，不必逐 job 上傳 `.py`。可先使用明標的可信多檔範例熟悉流程。
3. 選執行入口，例如 `main.py` 或 `train.py`。參數每行一個；含空白的單一參數留同一行。專案根目錄預設選取的資料夾根；進階可選其內相對子目錄，入口必須位於該根目錄內。
4. 環境與資料位置可設定一次供重用。環境需填具名稱的遠端絕對 `.sif` 路徑；沒有資料也可先選「待核對環境」離線準備。資料集/模型各以具名稱的國網既有路徑登錄，不在本機讀取或驗證存在。**每一個登錄都是未連線核對**，不是帳號、runtime 或路徑可用性證明。
5. 選「建立工作」，選專案、調整這次參數與資料位置，選簡單資源範本。主機由 Slurm 分配，不手填節點。
6. 選「準備工作」。工作固定專案版本與程式 bytes/雜湊、環境/位置、參數/資源；看到「已準備，尚未提交國網」。正式提交仍不可用；腳本/工作包下載放在進階，離線準備不需要先理解 ZIP/Slurm/SIF。
7. 修改專案會產生新版本，不改已建立草稿或工作。工作詳情可查看狀態/結果，或「複製建立新工作」重用原始快照與設定；產生新的草稿/意圖，不重新提交原工作。

已準備的草稿不可修改；用複製建立下一份。登錄的環境與資料位置也是不可變紀錄，變更位置時新增一筆、再建立新專案版本或工作。沒有自動 Git pull、套件安裝、模型下載或上傳程式執行。

## 資源與成本

| 範本 | 預設配置 |
|---|---|
| 短程測試 | dev／H100，1 GPU，20 分鐘 |
| 一般訓練 | normal／H100，1 GPU，60 分鐘 |
| 較大 GPU 記憶體 | normal2／H200，1 GPU，60 分鐘 |

所有範本都是單節點/單 process；1–8 GPU、1–16 CPU、1–128 GiB memory。dev 最長 120 分鐘，normal/normal2 最長 2880 分鐘；不支援 4nodes、torchrun 或多節點自動訓練。多 GPU 使用方式由程式負責。範本不依模型名稱猜 VRAM，不承諾排隊時間、partition 權限或容量。

費率類別未知時顯示待確認，以最高公開 GPU 費率檢查使用者設定的費用上限；不是國網帳務硬性額度。不含 HFS 儲存費，餘額/期限/實際費率未核對。計畫 ID、通知 email、CPU/memory、費率類別與上限屬進階設定。

## 程式與大型資料分開保存

程式快照限 **16 MiB／100 檔**。這是小型程式專案上限，不是大型 dataset/權重/checkpoint 搬運方案。更新資料夾會明確產生整份新快照，舊版本保留。

`.git/.hg/.svn/.venv/venv/__pycache__/.cache/.pytest_cache/.mypy_cache/.ruff_cache/node_modules/.ssh/.aws/.azure/.gnupg/.codex/.agents/keys`，`.env*`、`credentials/secrets/token/password`（含副檔名）、SSH key（含 `.pub`）、`.pem/.key/.p12/.pfx/.jks/.keystore` 與 `.pyc/.pyo` 等不匯入；UI 列出排除檔案與原因，已知秘密檔案在瀏覽器編碼前排除，伺服器再次檢查。所有相對路徑仍須有效 UTF-8/NFC、1024 bytes/路徑及 240 bytes/片段，拒絕 traversal、控制/隱藏方向字元、大小寫/前綴衝突，已知 PEM 私鑰內容亦拒絕。不是完整秘密掃描器，匯入前自行整理程式資料夾。

國網資料參照限 8 個/工作，類型為 dataset/model；絕對路徑限英數字、`_`、`-`、`.`、`/`，不接受逗號、冒號、空白、`.`/`..` 片段或憑證目錄。只存 metadata，不檢查本機同名路徑，不能假裝資料已存在。

state payload 邏輯合計 256 MiB，含所有專案版本、草稿、工作包、成果；每類最多 100 個專案/草稿/工作/環境/位置，每專案最多 100 個版本。SQLite/WAL/metadata 磁碟量另計；達上限明確拒絕，不自動刪除舊資料。結果匯入/輸出仍為 16 MiB／100 檔；大型成果/checkpoint 取回尚未支援。

## 腳本與路徑契約

程式在容器內的**專案根目錄**作 cwd，入口相對此根執行，因此同目錄子模組與相對設定檔可用。工作包保留 `inputs/code/<原相對階層>`；預設 cwd `/workspace/inputs/code`。進階選定子目錄後，它作專案根，入口也相對該目錄執行。

腳本使用官方 `PROJECT_ID` account、單節點 `srun singularity exec --nv --cleanenv --no-home`。程式工作區 `/workspace` 唯讀 bind，遠端資料逐項唯讀 bind 為 `/batch-assets/a1` 等；成果獨立 rw bind `/batch-output`。不下載/安裝、不自提交。缺 account/SIF 或無 `SLURM_JOB_ID` 時拒絕，避免在登入節點直接運算。輸出位置 `outputs/<本機工作ID>-<SLURM_JOB_ID>` 必須是全新目錄，存在就失敗，不覆寫。

| 程式環境變數 | 值與用途 |
|---|---|
| `BATCH_INPUT_DIR` | `/workspace/inputs`，工作包內輸入 |
| `BATCH_OUTPUT_DIR` | `/batch-output`，本次工作獨立成果目錄 |
| `BATCH_ASSET_1` … | `/batch-assets/a1` …，依工作快照位置清單順序 |
| `BATCH_DATASET_DIR` | 第一個 dataset 位置；沒有就不設定 |
| `BATCH_MODEL_DIR` | 第一個 model 位置；沒有就不設定 |

manifest 保存所有位置名稱、類型、路徑及順序，均 `verified:false`。SIF、Python/CUDA/套件、實際 filesystem/bind 權限與 SBATCH flags 尚待真機驗證。可信多檔 sample 的有界 CPU 驗證只證明封裝/根目錄/相對 imports/config 契約，不執行使用者匯入程式，也不取代容器/GPU驗收。

## 結果、示範與舊資料

工作詳情分開顯示運算狀態與結果可用性。手動結果 ZIP（stored/deflate、無密碼/symlink/特殊檔案）匯入來源標 `manual_import`，不改運算狀態。已有成果不可覆寫；稍後狀態改為完成仍保留原 bytes/來源/下載。UTF-8 純文字預覽最多 64 KiB，HTML 當文字；單檔 attachment 保留中文 basename/副檔名，二進位僅供下載。

離線示範在獨立次要入口，使用固定有界 CPU `sum(0..9)=45` 與傳輸 bytes/hash 演練，不執行使用者程式、GPU 或訓練。示範紀錄與舊版工作不冒充真實排程工作。unknown 意圖持久保存不能重送；取消請求不證明停止；fixture 確認只代表本機示範，沒有 Slurm 查證。沒有 background polling/watch/squeue loop。

v1 草稿/工作/成果保留，舊工作可讀/下載，明標舊版且不杜撰專案來源。v1 工程歷史見 [原驗證](../evidence/2026-10-02-nano5-batch/README.md)，v2 驗證/備份/rollback 見 [本輪紀錄](../evidence/2026-10-02-nano5-v2/README.md)。

## 保存、遷移與安全

獨立 state `<state>/batch.sqlite3`，0700 目錄/0600 DB，禁止 symlink；上傳 bytes 留 SQLite，不寫成可執行檔。停止後以同一 state 重啟保留資料。備份先停止本人 UI process，保存整份 state，不單複製仍寫入的 SQLite 主檔。v2 是 additive schema，舊資料不改寫；遷移前試副本並核對行數/內容雜湊、integrity/foreign_keys/舊下載。

HTTP 保留 Host/Origin/Sec-Fetch-Site、CSRF、JSON 大小/時間上限、CSP 與安全下載。CLI audit hook 阻擋 outbound DNS/connect/sendto 及程序啟動；`--verify-offline` 在系統呼叫前證明拒絕，沒有實際國網探測。此措施不是任意 Python 的 OS sandbox；使用者程式只當資料。本機 OS 管理者可讀 state，沒有多使用者/RBAC/DB 加密設計。

## 官方依據與下一階段

公開依據研究 2026-10-02（臺灣），來源包括 [Nano5 手冊](https://man.twcc.ai/@AI-Pilot/manual)、[官方 batch](https://man.twcc.ai/h5-FcgfRSSGSq3Chz8BTwA)、[2026/07/01 partition](https://man.twcc.ai/aPiCU8VXS7SZgFJBOoSqBQ)、[Singularity](https://man.twcc.ai/RWw-IinmS7euMwZ4edVv1g)、[SFTP](https://man.twcc.ai/Yg_dk6n2T_-Y2Pmx3fXzAA)、[使用注意事項](https://man.twcc.ai/CxQR2qnESZ-n_7vnCklX1g)、[iService 公開費率](https://iservice.nchc.org.tw/nchc_service/nchc_service_qa.php?target=54)。H100 25/50/50/120、H200 30/60/60/150 元/GPUhr（國科會/學術/政府法人/企業個人），HFS 另計。沿用同日已取得的官方全文，不把範例 SIF 或登入畫面當可用性證據。

下一階段需 main 確認人類在場與精確範圍，再唯讀核對 IP/帳號規範、計畫 ID/費率/餘額/期限、partition 用量、SIF/runtime、資料路徑及 bind/輸出權限；先審查精確工作快照與費用上限，另授權有界付費測試。認證/OTP 由使用者輸入不保存；單次提交保存真實 Job ID/receipt，結果不明先對帳，取消須有終止證據。不從本輪工程交付推定真機授權。

## 開發追溯

固定指南 `8463b05ba165e97cb11e43ddc320a522937ffc68`；規格/授權/角色見 [v2 規格](nano5-v2-spec.md)。工作分支 `feat/nano5-batch-ui`、基線 `e1f3c2e6b3a2b6744142cb037b3e63a35d2c4f39`。本輪未提交/push/merge/release/deploy。重現 CPU：`uv run --locked pytest -q`。工程通過不等於使用者接受或真機正常。
