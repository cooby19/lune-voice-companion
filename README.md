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

> **測試階段可略過本節。** `config.toml` 的 `models.provider` 預設為 `local_qwen`，
> 對話完全在本機推論：不需要 API key、不連網、不產生費用，`lune` 也不會要求 Keychain。
> 需要雲端時把 `provider` 改成 `"openai_responses"` 再依下列步驟設定。決策背景見
> `docs/project-decisions.md` 的「測試階段的 LLM 組成」。

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

### 規劃中的本地 LLM 實驗

目前 release 實作仍使用上述 OpenAI Responses provider，尚未提供可選的本地文字 LLM。
專案已決定在 M6 完整 pipeline 組裝前，先於 MacBook Air M4／16GB 評估官方
[`Qwen/Qwen3.5-4B`](https://huggingface.co/Qwen/Qwen3.5-4B) 的 Q4 量化版本。第一輪只測官方
post-trained 模型，不使用 Roleplay fine-tune，也不把實驗結果描述為已取代 OpenAI。
尺寸選 4B 而非更大的 9B，是因為 16GB 統一記憶體要同時容納 Whisper、E5 與 TTS，且被動
散熱的 Air 需留熱餘裕；先測 4B 可用較低成本判斷本地路徑是否可行。

Q4 的模型格式與 runtime 尚未選定；候選必須先通過 non-thinking 串流、首 token 與端到端
延遲、16GB memory pressure／swap、30 輪穩定性、中央取消、三句 gate，以及
`propose_memory`／`propose_affinity` 工具呼叫 gate。模型可載入或能輸出中文，不等於可以作為
即時語音 release backend。實驗前不會由 Lune 自動下載模型；模型 revision、檔案 checksum、
offline 載入與本機 endpoint 邊界會在選定 runtime 後另行固定。

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

## 本機語音合成

M5 將所有本機語音後端固定在同一套 streaming 契約：每個完整 utterance 會建立一筆帶有
`request_id`／`generation_id` 的請求，後端只回傳帶 generation 的 signed 16-bit interleaved
PCM。Router 只在 utterance 開始前選擇一次後端；GPT-SoVITS 若在第一個 PCM chunk 前失敗，
可由 `AVSpeechSynthesizer` 從頭合成，開始出聲後則不會在句中切換聲線。

`AVSpeechSynthesizer` 是 release fallback，也是私人 GPT 效能 gate 尚未完成時的固定預設。
它使用 Apple 的 buffer callback 取得 PCM，取消時使用 immediate boundary；不直接播放到系統
輸出，因此仍由 Lune 的 generation fence 與音訊 transport 管理播放。

`writeUtterance:toBufferCallback:` 只有在**主執行緒**的 `CFRunLoop` 運轉時才會送出 buffer，
而 engine 是純 asyncio 程序。目標機實測：主執行緒 run loop 有被 drain 時可取得 271 個 buffer，
完全不 drain 時為 0 個，把 run loop 放在其他執行緒同樣為 0 個；callback 一律送到主執行緒，
與呼叫 `writeUtterance:` 的是哪條執行緒無關。因此 driver 透過 `MainRunLoopPump` 在 asyncio
迴圈內以非阻塞方式 drain 主 run loop，並在沒有資料時自動放慢。

AVSpeech 會把整段 utterance 以爆發方式一次寫出，而不是即時串流：一句約三秒的中文會在
consumer 被排程之前就送出 271 個 buffer，因此 adapter 的 bounded queue 預設放大到 512
（約十秒、約 0.5 MB），仍會對異常長的輸入 fail closed。

以 Lune 自己的 adapter 實測（僅寫入記憶體，未開啟輸出裝置）：中文冷啟動 TTFA 450 ms、
暖啟動 119 ms；英文 280／118 ms；中英混流 121／118 ms；取消在第一個 chunk 後即停止。

實驗性 GPT-SoVITS runtime 固定到官方 commit
[`48b1a0169a28582a8984402f82cf438d3bfa6aca`](https://github.com/RVC-Boss/GPT-SoVITS/tree/48b1a0169a28582a8984402f82cf438d3bfa6aca)，
並須由使用者自行放在
`~/Library/Application Support/Lune/models/gpt-sovits-runtime/`。目錄內的 `.lune-revision`
必須只有該 40 字元 commit；repo 不提供 downloader，也不會自動取得 runtime、checkpoint、
參考音訊或參考文字。私人 voice manifest 仍放在
`~/Library/Application Support/Lune/voices/gpt-sovits/manifest.json`，並由 M0.5 validator 與
worker 啟動時各自核對 revision、regular file、權限與 SHA-256。

Worker 使用獨立 Python 3.10、stdin/stdout 長度前綴協定及 stderr-only upstream log；環境採
allowlist，不繼承 API key，Transformers／Hugging Face 固定 offline。每次啟動都主動驗證
`sandbox-exec` 確實拒絕檔案 canary 與 network，再以 deny-by-default profile 啟動；工具已被
Apple 標為 deprecated，因此 probe 或 profile 套用失敗一律停用 GPT 並 fallback。取消先要求
worker soft stop，500 ms 未完成才終止目前已驗證的 worker PID；重建失敗會開啟 session
circuit breaker。

Checkpoint checksum 只能確認檔案與私人 manifest 相符，不能排除 pickle／`torch.load` 的
任意程式碼執行風險。私人 GPT 模型的 TTFA、RTF、RSS、thermal 與取消 gate 尚未執行，
因此即使設定要求 `gpt_sovits`，release factory 仍會保持 `avspeech`，直到該 gate 有明確通過
證據。

## 完整語音管線與中央取消

M6 把 M1–M5 的元件組裝成唯一一條路徑：

```text
LocalAudioTransport.input → VoiceTurnGate → LuneFinalOnlySTTService
  → ContextEnricher → LLM provider → SentenceGate → TTSRouterService → PlaybackSink
```

`GenerationCoordinator` 是唯一能作廢一個 generation 的入口。它先同步遞增 generation ID，
讓仍在飛行中的 token、工具呼叫與 PCM 立即成為舊資料，接著依固定順序拆除：先停止可聽輸出
（這段耗時就是 200 ms 插話門檻量測的值），再作廢 STT、對 provider 送出 Pipecat
`InterruptionFrame` 並排空、撤銷未提交的工具提案，最後處理 turn gate 與 transport。

插話是唯一允許音訊跨越 fence 的情況：打斷 Lune 的那段語音本身就是下一句話，因此 turn gate
會保留進行中的 utterance 並重新標記 generation，同時在新 PCM 追上前暫時接受被打斷 generation
的殘留資料。其他原因（裝置切換、STT 逾時、輸出佇列溢位）一律丟棄。

所有佇列都有上限：輸出佇列滿載會取消該次生成而不是無限成長，輸入不連續會重建取樣時間軸而
不是產生錯位的音訊。工作狀態（`thinking`、`speaking`）綁定其 generation，取消後自動回到可
聆聽狀態，不會卡住麥克風。

M7 前半加入真正的 `CoreAudioStreamOwner`。PyAudio input callback 只把 signed-16-bit PCM 複製到
既有 bounded transport；CoreAudio／PortAudio 的查詢、開關 stream 與 blocking output write 由
單一 lifecycle owner 管理。輸出 write 會切成最多約 20 ms 的區塊，讓 `flush()` 可在當前短區塊
後停止，而不必等完整大 chunk 播完。冷啟動不開 input stream，內建輸出維持
`paused_unsafe_output`，裝置切換仍先經中央 generation cancellation 才重建。

`lune.engine` 只呼叫 `build_voice_pipeline` 組裝上述唯一管線，並管理 input pump、預設裝置監看、
stream recovery 與關閉順序。AVSpeech 仍在 engine 主執行緒的 asyncio／CFRunLoop 上運作；沒有把
run loop 移到已知收不到 callback 的 worker thread。

上述 adapter 與 engine 目前只通過 fake CoreAudio／PortAudio、deterministic VAD／STT、scripted
provider、fake TTS 及 recording output 的公開 gate。**實體麥克風、耳機、裝置切換與 200 ms
插話 gate 尚未執行**，不得把公開測試視為硬體證據。

取消後不得播放的內容也不會落庫：使用者逐字稿在 final 接受後保存，assistant 內容只保存確實
送到輸出裝置的句子，記憶與 affinity 提案只在該 generation 仍有效時提交。

端到端延遲定義為「最後一個有聲輸入 sample → 第一個非靜音輸出 frame」，量測與門檻邏輯放在
`lune.pipeline.benchmark`。30 輪暖機 benchmark 需要實體麥克風與本機模型，**尚未執行**；依
既有實測拆解，原訂 p50 ≤1.5 s 在目標硬體上無法達成，詳見
[`docs/progress.md`](docs/progress.md)。

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
