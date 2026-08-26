# Lune 即時語音陪伴

Lune 是一個 local-first、原生支援 Apple Silicon 的 macOS 選單列語音陪伴。
語音、逐字稿、人格、記憶、關係狀態與私人聲線資產都留在 Mac；只有產生回覆
所需的最少文字內容會傳送給 OpenAI。

本專案目前是持續開發中的 MVP。每次冷啟動時麥克風預設關閉；若輸出切換為
Mac 內建喇叭則自動暫停，因為 MVP 尚未實作聲學回音消除（AEC）。未放置私人
GPT-SoVITS 資產時，系統會改用 `AVSpeechSynthesizer`，仍可正常啟動。

## 系統需求

- Apple Silicon Mac
- macOS 14 或更新版本
- Homebrew 套件：`uv`、`portaudio`、`ffmpeg`
- Engine 使用 Python 3.12
- 只有啟用實驗性 GPT-SoVITS worker 時，才需要另一套 Python 3.10 runtime

## 本機安裝

```sh
brew install uv portaudio ffmpeg
uv python install 3.12
uv sync --frozen
uv run lune doctor
```

Lune 將私人狀態存放於 `~/Library/Application Support/Lune/`，安全診斷記錄則
存放於 `~/Library/Logs/Lune/`。首次設定時，請手動複製範例：

```sh
mkdir -p "$HOME/Library/Application Support/Lune/persona"
cp examples/config.example.toml "$HOME/Library/Application Support/Lune/config.toml"
cp examples/kernel.example.yaml "$HOME/Library/Application Support/Lune/persona/kernel.yaml"
```

範例人格刻意保持一般化；正式環境不會偷偷套用它。啟用麥克風前，請先編輯
Application Support 中的私人副本。

## OpenAI API 設定

1. 登入 [OpenAI API Platform](https://platform.openai.com/)，建立或選擇組織下的
   Project，並設定計費與 Project 支出上限。
2. 前往 [API keys 頁面](https://platform.openai.com/api-keys)建立 secret key。
3. 執行 `lune key set`，直接將 key 存入 macOS Keychain。命令會以安全輸入方式
   提示；禁止將 key 當作命令列參數、貼到聊天，或寫入 `config.toml`。
4. Lune 透過 [Responses API WebSocket](https://developers.openai.com/api/docs/guides/websocket-mode)
   傳送文字，並關閉 response storage。使用前請閱讀 OpenAI 的
   [API 資料控制說明](https://developers.openai.com/api/docs/guides/your-data)。

預設模型為 `gpt-5.6-terra`。只有在第一句尚未送往播放前發生一次暫時錯誤，
或本機每月費用策略達到警戒線時，才改用 `gpt-5.6-luna`。

## 隱私與私人聲線資產

禁止提交 `kernel.yaml`、`config.toml`、資料庫、金鑰、裝置識別資料、完整訪談、
參考錄音、checkpoint 或模型下載檔。私人 manifest 中的 Firefly 檔名與 hash
只供個人本機使用；本 repo 不會下載或重新散布這些資產。Checksum 可以偵測檔案
是否改變，但無法證明 pickle-based checkpoint 是安全的。

診斷記錄只包含狀態轉換、有限的錯誤代碼、耗時與彙總資源數據；不得包含逐字稿、
prompt、人格內容、記憶、API key 或完整檔案路徑。

## 開發與驗證

```sh
uv sync --frozen --extra build
uv run ruff check .
uv run mypy src/lune
uv run pytest
uv run python scripts/secret_scan.py
```

硬體與模型驗收 gate 必須明確選擇後才會執行。Mock 測試與套件 self-test 不下載
模型，也不使用 API key。詳細狀態與決策請見：

- [`docs/progress.md`](docs/progress.md)：里程碑與 gate 證據
- [`docs/project-decisions.md`](docs/project-decisions.md)：淨化後的公開決策

## 疑難排解

若 macOS 上的 `uv run` 回報 `No module named 'lune'`，請檢查 `.venv` 是否被套用
遞迴 hidden flag。Python 3.12.13 會忽略帶有該 flag 的 editable `.pth`；以下命令
只清除虛擬環境的 hidden metadata，不會刪除任何檔案：

```sh
chflags -R nohidden .venv
uv run lune self-test
```

## 授權

程式碼採 MIT License。私人 persona 與聲線資產不屬於此授權，也不屬於本 repo。
