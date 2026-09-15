# 第一個模型候選調查（2026-09-15）

實機：RTX 5090 Laptop GPU，24,463 MiB，driver 581.57。模型服務未啟動前 GPU 實體用量約 3 GiB。Windows 程序盤點主要是 DWM 與桌面應用；WSL 沒有推論引擎，現有四個 KaChing 容器沒有已確認的 GPU 推論工作。沒有以「釋放 GPU」名義關閉桌面／文件／其他專案。

| 候選 | 本機權重 | 特點 | 本次建議 |
|---|---:|---|---|
| Qwen/Qwen3.5-4B | 約 9.3 GB | 視覺、工具呼叫；已有一致 revision | 第一輪候選，留有較多 runtime/視覺/KV 餘裕 |
| google/gemma-4-E4B-it | 約 16.0 GB | 視覺、音訊、function calling | 可後續比較；E4B 名稱不代表整個 checkpoint 只有 4B×2 bytes |
| microsoft/Phi-4-mini-instruct | 約 7.7 GB | 文字；原先純文字 smoke 候選 | 不符合追加的多模態偏好 |
| google/gemma-4-12B-it | 未盤點到 | 更新的本地多模態／agentic 模型 | 需另選量化與下載、驗證 vLLM 配套 |
| Qwen/Qwen3.8-27B | 未盤點到 | 更新的視覺與 agentic 候選 | 原始權重不適合直接載入目前 24 GB GPU |
| nvidia/Qwen3.8-27B-NVFP4 | 未盤點到 | 官方 NVIDIA 混合 NVFP4/FP8 量化，模型卡列文字／影像／影片 | 有潛力，但模型卡使用 GB300、多 GPU 與 nightly 範例；不能當作此筆電已驗證 |

目前建議以既有 Qwen3.5-4B 取得完整服務驗收，再對較大新版模型設計量化、context 與能力比較。這是部署與驗收成本取捨，不是宣稱 Qwen3.5-4B 為所有任務 SOTA。

來源（於本次調查讀取官方頁面）：

- https://huggingface.co/Qwen/Qwen3.5-4B
- https://huggingface.co/google/gemma-4-E4B-it
- https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/
- https://huggingface.co/Qwen/models
- https://huggingface.co/Qwen/Qwen3.8-27B
- https://huggingface.co/nvidia/Qwen3.8-27B-NVFP4

模型卡的 benchmark 屬供應方報告；此次不做排名重現，不將小型 synthetic smoke 當作 agentic 成功率或產品品質證據。
