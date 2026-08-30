---
name: physical-gate
description: 在 Lune repo 執行需要實體裝置或私人資產的驗收：麥克風、耳機／實體輸出、裝置切換、私人 persona 或語料、私人模型權重、雲端 API。要跑實體 smoke test、量延遲、驗證硬體行為，或任何公開 CI 無法構成證據的 gate 時使用。
---

# physical-gate

公開 CI 全部使用 deterministic fake，**永遠不能當作硬體、私人模型或雲端的證據**。
這個流程處理的就是那些必須在真實條件下取得證據的驗收。

## 1. 先逐項取得授權

以下每一項都是**獨立**的授權，不能互相推導，沉默不等於同意：

- 開啟麥克風
- 耳機／實體輸出裝置，以及裝置拔除／切換
- 載入本機 Whisper 與 E5 權重
- 載入本機 LLM 權重
- 讀取私人 `config.toml` 與 `persona/kernel.yaml`
- 讀取私人語料或錄音
- 發出雲端請求或使用 API key（可能產生費用）

取得前不讀取私人設定、模型或裝置內容。授權範圍只涵蓋這次，不自動延伸到下一次。

## 2. 記錄量測環境

在開始前確認並記下：

- **低電量模式是否關閉**
- 當時的 load average
- 電源狀態

延遲數字沒有這兩項就**不可比**：實測低電量模式讓同一份語料的 Whisper 暖狀態從
1,826–1,889 ms 變成 6,676–7,290 ms，與程式無關。任何引用延遲的句子都要附上這個脈絡。

## 3. 走既有的路徑，不要另接一條

```sh
uv run python scripts/run_physical_voice_smoke.py    # preflight / turn / barge-in / device-switch
uv run lune-engine --microphone                      # UI 之外唯一能開始對話的方式
uv run lune-engine --ephemeral-memory                # 讓 shakedown 不寫入真實資料庫
```

實體驗收必須用 `build_voice_pipeline` 組出來的**同一條 production 管線**；不得為了方便
另接簡化路徑。量音訊指標時用 scripted provider，避免模型抖動污染音訊數據。

## 4. 證據的形狀

- 只保留**數值與狀態碼**：沒有音訊、沒有逐字稿、沒有 prompt、沒有私人路徑、沒有裝置 UID。
- 報告寫入 `~/Library/Logs/Lune/`，檔案權限 `0600`。未淨化的報告不進版控。
- 私人 persona rubric 的題目與回答不進公開 repo 或診斷。

## 5. 門檻

- **門檻不得為了通過而放寬、修改或刪除。** 未通過就保留失敗證據並回報。
- 門檻要改、或某階段要明示豁免，是**產品決策**，需要使用者做出並記進
  `docs/project-decisions.md`，原始門檻值與失敗證據原樣保留。
- 現行主要門檻：端到端 p50 ≤1,500 ms／p95 ≤2,200 ms、插話停止可聽輸出 ≤200 ms。
  端到端定義為「最後一個有聲輸入 sample → 第一個非靜音輸出 frame」，起點由 transport 的
  取樣錨點反推，不可由處理時間推導。

## 6. 記錄結果

更新 `docs/progress.md`，把**已通過／未通過／未執行**三類分開列，附實測值與門檻。
未執行的項目要寫清楚缺什麼（授權、資產、硬體）。

## 完成條件

授權齊備、環境已記錄、跑的是 production 管線、證據已淨化、門檻原樣保留、
`docs/progress.md` 已分類更新。任何一項缺席就標 `BLOCKED` 或 `MANUAL-VERIFICATION-REQUIRED`，
不要用推測補上。
