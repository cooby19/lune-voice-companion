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
Responses request 固定使用 `store=false`、`reasoning.effort=none`、最多 192 output tokens 與
standard pricing 對應的 `service_tier=default`。句數 gate 最多放行三個完整中英文句子；第三句
完成後會取消並排空剩餘 generation。

費用控制以 Asia/Taipei 月界線、固定匯率與每次 worst-case reservation 計算；預留後達
NT$700 改用 Luna，達 NT$900 則不再發出雲端請求。取消、錯誤或不完整回覆若缺 usage，會以
完整預留額保守估算。每筆 settled attempt 都寫入 SQLite，重新啟動後會先還原當月已確認成本，
再接受新的 reservation。

## 本機語音辨識模型

M2 固定使用
[`mlx-community/whisper-large-v3-turbo-q4`](https://huggingface.co/mlx-community/whisper-large-v3-turbo-q4/tree/660c343bbf4e52ac257f0b7d952e5388e6f93bef)
的 immutable revision `660c343bbf4e52ac257f0b7d952e5388e6f93bef`。Runtime 不會把 Hugging Face
repo ID 交給 `mlx-whisper`，也不會隱式下載模型；缺少 optional dependency、manifest 或任一
模型檔時，只會回報 `setup_required`。

模型目錄固定為 `~/Library/Application Support/Lune/models/whisper/`，只允許目前使用者
存取。放置 `config.json` 與 `weights.npz` 後，建立權限 `0600` 的 `manifest.json`：

```json
{
  "schema_version": 1,
  "model_id": "mlx-community/whisper-large-v3-turbo-q4",
  "revision": "660c343bbf4e52ac257f0b7d952e5388e6f93bef",
  "files": [
    {
      "relative_path": "config.json",
      "sha256": "538e24557b8f9bc504700add5e7bbe32087c2353001ff563e64772ad4398671a"
    },
    {
      "relative_path": "weights.npz",
      "sha256": "862bbc832b05f3f4ec19dd632b701d61a6d3f5c7906360a10d72a79870642a80"
    }
  ]
}
```

`mlx-whisper` 只在第一次真實推論時 lazy import；基本安裝、公開測試與 module import 都不需要
`mlx` extra。同步 native inference 會在背景 thread 自然完成；generation 變更或 `close()`
會作廢結果，但不宣稱能強制終止已進入 native code 的 thread。

## 本機記憶與檢索

M4 將 conversation、rolling summary、long-term memory、relationship audit 與 LLM usage 存在
`~/Library/Application Support/Lune/lune.sqlite3`。SQLite connection 固定啟用 foreign keys、
WAL、5 秒 busy timeout 與 `secure_delete`；資料目錄與資料庫分別使用 `0700`／`0600`。
只有 final user transcript 與已確認播放的 assistant 文字會進入完整 turn；取消的摘要與工具提案
不會落庫。

本機 semantic retrieval 固定使用
[`intfloat/multilingual-e5-small`](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3)
revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`。Runtime 只接受本機模型目錄、停用 remote
code、要求 safetensors，並核對 `model.safetensors` SHA-256
`1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477`。模型目錄固定為
`~/Library/Application Support/Lune/models/e5/`，其 `manifest.json` 格式如下：

```json
{
  "schema_version": 1,
  "model_id": "intfloat/multilingual-e5-small",
  "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
  "files": [
    {
      "relative_path": "model.safetensors",
      "sha256": "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477"
    }
  ]
}
```

搜尋使用 `query:`／`passage:` 前綴、384 維 cosine full scan、top-k 5、0.72 門檻與最多
1,200 字回填。管理 CLI 不提供 bulk clear；搜尋詞以互動提示輸入，單筆刪除必須再次輸入 exact
ID 確認：

```sh
uv run lune memory list
uv run lune memory search
uv run lune memory export
uv run lune memory forget <exact-id>
```

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
- [`docs/handoff-m2-m8.md`](docs/handoff-m2-m8.md)：M2 public gate 後的 M3–M8 實作規劃與交接

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
