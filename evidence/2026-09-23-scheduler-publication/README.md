# 2026-09-23 scheduler 候選發布

本次為 opt-in 開發候選發布；完整 managed GPU 驗收未完成且 GPU 工作暫停。
目標、契約、歷史證據與協調入口見 [main2 交接](../../docs/handoff-main2.md)。

## 發布版本

三個 linux/amd64 候選均以乾淨 source
`c7f35e14911eb8a4bb12458b0c79bb2ef72f6351` 建置，tag 為
`scheduler-dev-20260923-c7f35e14911e`。候選 source 已合入遠端 main 的 Linux static
文件 `6c87816bb0c5289cb87bfa327dadadd3d954e830`（merge `89799fa`）。
後續發布 receipt／main2 方向補充是 docs-only，不重建 images；本目錄所在交付 commit
可由 Git history 定位。package 版本仍為 0.2.0，候選 tag 與 OCI release-channel 明確標示
`candidate`，GPU acceptance label 為 `incomplete-paused`，不是穩定版宣告。

| Repository | 遠端 OCI index digest（亦為已測本機 image ID） |
|---|---|
| `momonong/selfhost-models-api` | `sha256:f4e410e8a097d1c53d2ea8bbb36a48767b309a548bb432dfaf9fcba253908b2a` |
| `momonong/selfhost-models-vllm` | `sha256:fdebb477430924ab768ce6db0d2294f2f8d5c693abd5589275fb9fcfb2819655` |
| `momonong/selfhost-models-whisper` | `sha256:fcce6a4a9bdb31edbb2b2270d0bb05211ebb06bf50b68372ea966009595a4953` |

例如固定取得 API 候選：

```bash
docker pull momonong/selfhost-models-api@sha256:f4e410e8a097d1c53d2ea8bbb36a48767b309a548bb432dfaf9fcba253908b2a
```

發布前經 authenticated registry lookup 確認三個 tag 均為 404/MANIFEST_UNKNOWN；發布後
以匿名 registry 讀取確認公開可取得，index／linux-amd64 manifest／config digest、OCI
revision／channel 與 entrypoint 均已核對。完整值在 [candidate-tags.json](candidate-tags.json)，
建置 metadata 在 [builds.json](builds.json)。runtime base 與 locks 沿用固定版本，沒有模型下載。

未重發 static Transformers：有效 entrypoint、internal import closure、Dockerfile 與
runtime locks 相對既有 source `29bec68` 完全相同，逐檔 hash 見
[transformers-no-update.json](transformers-no-update.json)。API／vLLM／Transformers 的
`0.2.0` 與 `latest` 六個標籤，發布前後 index、platform、config 與 metadata 完全一致，
見 [stable-tags.json](stable-tags.json)。

## 驗證與限制

- [Windows 完整 CPU 契約](contract-tests.json)：Python 3.12.14／uv 0.11.21，
  `186 passed in 21.75s`。測試後至 candidate source 只有文件／證據變更。
- [候選容器 CPU 檢查](cpu-images.json)：三個 image 都用 explicit runc、network none、
  無 GPU device request、read-only root、2 CPUs／2 GiB、128 pids、64 MiB tmpfs；
  無模型／state host mount、無網路、CUDA 未初始化，model loads／GPU generations 均為0。
- API 驗證 auth、無模型 catalog、models/ready 503、嚴格 job 輸入及 SQLite fixture；
  worker 驗證 independent key、route allowlist、relay body／epoch；Whisper 使用合成 WAV
  與 CPU engine double，驗證取消後 compute 保留、capacity 429 與 token bound。
- 容器實際 bytes 與 source working tree 完全一致，並將 CRLF 正規化後對照 Git blob；
  分別核對 API 18、vLLM 3、Whisper 27 檔，見 `source-*.json`。
  CPU receipt 分別記錄當次 probe manifest 的 `source_manifest_raw_crlf_sha256` 與
  發布 Git blob 的 `source_manifest_git_lf_sha256`，避免 Windows 換行轉換導致重算不符；
  此差異只關乎 manifest JSON 的換行，image 內 code bytes 的逐檔核對未改變。
- Whisper 首次外部 probe 因 script import path 未含 `/app` 失敗；調整 probe 以符合
  正式 `python -m uvicorn` 的 cwd import context 後通過。未修改 image 或 runtime。

這些是 CPU／合成測試，並非新的 GPU、Whisper 辨識品質、完整 managed D 或人工驗收。
歷史 Qwen partial 結果與未捕捉 status/body 的 non-200 邊界見 main2 交接；沒有補跑或
修正 ready 疑點。沒有部署、恢復服務、GPU diagnosis/load/inference、重啟或 unload。

## 保留資源

[唯讀前後快照](retained-resources.json) 確認 API／controller 仍 inactive、repair ledger
仍 2 loads／18 generations 保守預留、lease0／model pin1；formal managed state 仍0／0。
既有 Qwen worker `5c5d1d848908` 與 relay `aa57278014b3`、原 static stopped containers、
舊 images／native state／model assets／ownership gate 均保留。persisted ready 並非即時
ready；13:33 Taipei GPU 樣本約 20,264／24,463 MiB，亦有其他 GPU containers 正在運行，
不是可用資源承諾。所有本輪 CPU probe 容器已退出並移除，未新增服務或 worktree。

候選 images、既有工作分支與 ignored 私人證據保留供追溯；不刪 branch、模型、DB、資料
或其他專案資源。私人 keys、audio、request、DB、完整 inspect 與個人路徑不納入本目錄。
