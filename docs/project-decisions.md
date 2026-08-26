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
  `store=false`，並刻意限制文字 context。
- Pipeline 只接收有型別的 provider frame；公開 CI 使用 deterministic fake provider。
- 唯一的 generation coordinator 在插話或輸出裝置切換後，統一作廢舊 STT、模型事件、
  工具提案與 PCM。

## 私人資料

- 私人資料根目錄：`~/Library/Application Support/Lune/`。
- 診斷記錄目錄：`~/Library/Logs/Lune/`。
- API key 只存於 macOS Keychain。
- SQLite 啟用 foreign keys、WAL、busy timeout、migration 與 secure deletion。
- 使用者逐字稿在 final 接受後保存；assistant 只保存已確認播放的部分；取消的文字與提案丟棄。
- 記憶只允許輸入 exact ID 並二次確認後刪除；不提供 bulk clear 命令。

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
