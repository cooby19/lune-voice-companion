# 專案決策

這是公開且已淨化的決策記錄，刻意排除擁有者的私人 persona、訪談內容、聲線錄音、
憑證、裝置識別資料與本機模型路徑。

## 產品邊界

- 平台為 macOS Apple Silicon，採選單列 UI 與 local-first 架構。
- 級聯語音路徑為：本機 VAD → 本機 Whisper → 純文字 OpenAI Responses → 本機 TTS。
- MVP 支援耳機，但沒有聲學回音消除；切換至內建喇叭時暫停聆聽。
- 每次冷啟動時麥克風保持關閉。不實作 login item、雲端同步、telemetry 或外部排程訊息。

## Runtime 與 provider

- Engine 使用 `uv` 管理的 Python 3.12、Pipecat 1.7.0 與 PyAudio／PortAudio。
- 主模型／備援模型為 `gpt-5.6-terra`／`gpt-5.6-luna`，使用 Responses WebSocket、
  `store=false`、`reasoning.effort=none`、最多 192 output tokens 與 standard pricing 對應的
  `service_tier=default`，並刻意限制文字 context。
- Pipeline 只接收有型別的 provider frame；公開 CI 使用 deterministic fake provider。
- 唯一的 generation coordinator 在插話或輸出裝置切換後，統一作廢舊 STT、模型事件、
  工具提案與 PCM。

## STT 模型與取消邊界

- STT 固定為 `mlx-community/whisper-large-v3-turbo-q4` revision
  `660c343bbf4e52ac257f0b7d952e5388e6f93bef`；`config.json` 與 `weights.npz` 的
  SHA-256 編譯於 public pin，私人 manifest 必須逐項相符。
- Runtime 只把驗證過的本機絕對目錄交給 `mlx-whisper`，不允許 repo ID 或隱式下載。
- 對外只輸出 final transcript。同步 native inference 進入 thread 後不假裝能強制取消；
  generation fence 會在接受、開始推論、推論回傳／拋錯與下游 callback 前作廢舊 epoch。
- 同時間只允許一個 running inference 與一個 pending request；pending 採同 generation
  latest-wins。`close()` 停止 Lune 管理的 asyncio worker，已進 native code 的 thread 自然結束，
  其結果不得輸出。

## 私人資料

- 私人資料根目錄：`~/Library/Application Support/Lune/`。
- 診斷記錄目錄：`~/Library/Logs/Lune/`。
- API key 只存於 macOS Keychain。
- SQLite 啟用 foreign keys、WAL、busy timeout、migration 與 secure deletion。
- 使用者逐字稿在 final 接受後保存；assistant 只保存已確認播放的部分；取消的文字與提案丟棄。
- 記憶只允許輸入 exact ID 並二次確認後刪除；不提供 bulk clear 命令。

## SQLite 記憶與關係狀態

- M4 schema 使用 `sessions`、`turns`、`messages`、`summaries`、
  `long_term_memories`、`relationship_state`、`relationship_events` 與 `llm_usage`；migration
  以 SQLite `user_version` 保持冪等。
- 最近最多 12 個未摘要 complete turns 進 prompt。第 13 個 complete turn 後，固定以
  `gpt-5.6-luna` 將最舊四個併入單一 rolling summary；coverage 只能連續向前延伸，取消或
  generation 失效時不得提交。
- E5 固定為 `intfloat/multilingual-e5-small` revision
  `614241f622f53c4eeff9890bdc4f31cfecc418b3`；`model.safetensors` SHA-256 為
  `1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477`。Runtime 僅從通過
  manifest 的本機目錄載入、停用 remote code 並強制 safetensors。
- 記憶工具採兩階段 proposal host；只有目前 generation 可提交，並以 normalized content
  去重。affinity 初始 50、全域 0–100、每 turn 最多一筆 ±1、每 session 累計最多 ±3，且每次
  變更保留 audit event。
- `llm_usage` 逐 attempt 保存 token 類型、價格版本、匯率、reserved／charged TWD 與是否估算；
  engine 重啟時只加總既有 charged TWD，不改寫舊價格。

## TTS 安全

- Release 永遠保留 `AVSpeechSynthesizer` fallback。
- GPT-SoVITS 是使用私人資產的實驗性 adapter，執行於 Python 3.10 獨立程序，並使用
  可拋棄 child、hash 驗證、環境清理、網路封鎖與 fail-closed sandbox。
- 本專案不下載或散布私人 checkpoint 與參考錄音。Pickle／checkpoint 的 hash 只能證明
  檔案身份，不能證明安全性。

## 費用策略

- 月份界線使用 `Asia/Taipei`，每次呼叫保存當時設定的固定匯率。
- 每個雲端請求都先預留成本；達 NT$700 後所有新請求改用 Luna，達 NT$900 後本機
  鎖定所有雲端請求。
- 取消後若 provider 沒有回報 usage，以完整預留額做保守估算。
- M3 使用 OpenAI `2026-07-30` standard price card：Terra 每百萬 input／cached input／output
  tokens 為 US$2／0.20／12，Luna 為 US$0.20／0.02／1.20；cache write 依官方規則以 uncached
  input 的 1.25 倍計。每筆 reservation 保存價格版本與匯率，之後價格更新不得回寫舊紀錄。
