# 專案決策

這是公開且已淨化的決策記錄，刻意排除擁有者的私人 persona、訪談內容、聲線錄音、
憑證、裝置識別資料與本機模型路徑。

## 產品邊界

- 平台為 macOS Apple Silicon，local-first 架構。UI 形態已由選單列常駐改為視窗應用程式，
  規格見 `docs/ui-spec.md`。
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

### 本地 LLM 前置實驗（2026-08-27 決定；2026-08-28 改為 4B；尚未實作）

- 已同意在 M6 組裝完整 pipeline 前，於目標 MacBook Air M4／16GB 上先評估官方
  [`Qwen/Qwen3.5-4B`](https://huggingface.co/Qwen/Qwen3.5-4B) 的 Q4 量化版本。官方模型名稱
  不含 `-Instruct`；該 repository 已是 post-trained 模型。
- 第一候選原為 `Qwen3.5-9B`，2026-08-28 改為 `Qwen3.5-4B`。理由是 M4 Air 為 16GB 統一
  記憶體且被動散熱：9B Q4 權重約 5GB，與 Whisper、E5 及 release TTS（M5 gate 上限
  6GB peak RSS）並存時記憶體餘裕過小；而端到端 p50 ≤1.5 s 扣掉 350 ms 句尾靜音、
  Whisper 推論與 TTS TTFA 後，留給 LLM 產出完整第一句的時間僅數百毫秒。先測 4B 是為了
  用較低成本回答「本地方案是否可行」，不是因為 9B 品質不足。
- 若 4B Q4 通過全部 gate 且仍有明顯記憶體與熱餘裕，可再評估 9B Q4 是否值得換取品質；
  若 4B 過不了延遲或穩定性 gate，視為本地即時路徑在此硬體上不成立，改採 hybrid 或維持
  OpenAI 是新的產品決策。兩個方向都不得自行降低門檻或靜默切換。
- 第一輪只測官方 post-trained 模型，不使用 Roleplay fine-tune。只有既有人格 prompt 無法
  通過 rubric，且候選 adapter／fine-tune 的來源、授權、chat template、safetensors 與工具
  呼叫能力都可驗證時，才另行評估微調版本。
- 「Q4」目前只代表量化等級；GGUF／MLX 格式、Ollama／llama.cpp／獨立 MLX worker 與是否
  增加第四個受管理程序都尚未決定。不得在 spike 前把任一 runtime 寫成正式架構。
- 語音路徑要求 non-thinking 回覆。`Qwen3.5-4B` 官方預設為 thinking 模式，需以官方 chat
  template 的 `enable_thinking=False`（或所選 runtime 的等效開關）關閉；實際是否可靠關閉
  必須在選定 runtime 後實測，不得假設支援。`<think>` 或 reasoning content 一律不得送入
  `SentenceGate`、TTS、記憶、SQLite 或診斷。
- 本地 provider 必須沿用 typed frame、generation／attempt correlation、三句 gate、兩階段
  memory proposal 與中央取消契約。若 runtime 只會關閉 client stream、不能證明停止本機
  推論，`remote_cancel` capability 必須如實標為 false。
- 16GB 統一記憶體是硬限制；驗收必須同時涵蓋 Whisper、E5 與 release TTS 的實際組合，
  記錄首 token 延遲、生成速度、peak memory、memory pressure／swap、thermal、30 輪穩定性、
  取消後 late token／tool call 與既有端到端 p50／p95。不得降低原門檻換取通過。
- 在 spike 與後續實作完成前，M3 的 OpenAI Responses、Terra／Luna、Keychain readiness、費用
  ledger 與 rolling summary 行為仍是目前實作；文件不得宣稱 Lune 已經 local-only。

### 本地 LLM spike harness（2026-08-28 實作；仍未安裝 runtime 或下載模型）

- 已建立 `lune.llm_spike`，只包含 gate 邏輯與公開 fixtures。它不安裝 runtime、不下載模型、
  不讀取私人 persona、不啟動任何程序，也沒有把本地 provider 名稱加入
  `lune.llm.contracts.ProviderName`。註冊本地 provider 是 M6 的事，且以 spike 結果為前提。
- 已依官方模型頁確認 `Qwen/Qwen3.5-4B` 存在且為 post-trained（base 版本是另一個 `-Base`
  repository），預設開啟 thinking 並以 `<think>…</think>` 標示，可用 chat template 的
  `enable_thinking=False` 關閉，且支援 tool calling。是否能可靠關閉仍須在選定 runtime 後實測。
- 官方 repository 沒有官方 Q4 產物。可選路徑是採用第三方轉換（例如 mlx-community 或 GGUF
  轉換）或下載官方權重後自行量化。兩者的下載量、信任邊界與可驗證性不同，屬於尚待授權的
  選擇；在選定前 `LOCAL_LLM_PIN` 維持 `None`，任何 manifest 檢查都 fail closed。
- runtime 候選以成本表記錄，不預先擇一：in-process `mlx-lm` 不增加 PID 但與 engine 共用位址
  空間；`mlx-lm` worker 可硬取消卻會成為第四個受管理程序；`llama.cpp` server 與 Ollama 需要
  loopback listener，其中 Ollama 是不受 engine 生命週期管理的系統常駐服務，與「退出後無
  orphan」及逐檔 pin 相衝突。
- 本地 endpoint 只允許 `http` 且 host 必須是 loopback、必須有明確 port，並拒絕帶憑證、query
  或 fragment 的 URL。
- 首句延遲門檻不獨立設定，而是由端到端 p50 ≤1.5 s 扣除 350 ms 句尾靜音、Whisper final 延遲
  與 TTS TTFA 推導。`SentenceGate` 以整句放行，因此端到端時鐘取決於第一個完整句子而非第一個
  token；兩者都會測量。M2／M5 的本機延遲尚未量測，因此推導結果為「未定」，而未定一律判定
  gate 失敗，不得視為通過。
- 記憶體驗收以可觀察的失敗徵狀為主：memory pressure 必須維持 `normal`、thermal 只允許
  `nominal`／`fair`、不得 OOM，且 RSS／swap／queue 不得單調累積或末段平均比首段惡化 25% 以上。
  另有預設 10 GiB 的合併 peak RSS 上限，這是 16 GiB 機器保留約 6 GiB 給 macOS、UI 與 page
  cache 的 spike 預設值，不是量測常數，使用者可另行設定。
- 取消分成兩個獨立問題：fence 關閉後是否仍有 token、tool call 或 PCM 流到下游（一律視為失敗），
  以及 runtime 是否真的停止運算（只有取得證據才可標示 `remote_cancel`）。只關閉 client stream
  仍可通過 gate，但不得宣稱 `remote_cancel`。
- 工具呼叫沿用 M4 的類別與限額：只允許 `propose_memory`／`propose_affinity`，每 turn 各最多
  一筆，delta 只能是 ±1，並以 normalized content 跨 turn 去重。本地模型不得比雲端模型獲得更
  寬的權限。
- `<think>` 濾除為串流實作，會處理跨 chunk 分割的標籤，且推理內容只計長度、不累積保存；
  出現任何推理內容都會記為違規，因為那代表 non-thinking 開關沒有生效。

### 本地 LLM spike 授權與選型（2026-08-28 由使用者決定）

- runtime 選定為 **獨立 `mlx-lm` worker**。使用者已明確接受它成為第四個受管理程序，理由是
  可用終止自己 spawn 的 PID 達成真正取消，且模型權重不進 engine 位址空間。這推翻了計畫原本
  固定的三 PID 假設，屬於已授權的程序架構變更。
- Q4 產物來源選定為 **官方權重 + 本機量化**：下載官方 `Qwen/Qwen3.5-4B` revision
  `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`，在本機量化為 Q4，再逐檔 SHA-256 建立 pin。
  不採用第三方已量化產物，以維持「官方 post-trained 模型」的信任邊界。
- 已授權讀取私人 persona 執行 12 題人格 rubric；題目與回答不進公開 repo 或診斷，只在本機
  產生 0600 報告。
- 實測發現：`Qwen/Qwen3.5-4B` 是 `Qwen3_5ForConditionalGeneration`，屬視覺語言模型，且採
  hybrid linear attention。`mlx-lm` 0.31.3 的 `qwen3_5` 支援它的 `text_config`，並在
  `sanitize()` 丟棄 `vision_tower` 權重，因此量化後的產物是純文字模型，語音路徑不會載入
  視覺塔。
- 實測發現：未登入的 Hugging Face 下載約 0.8 MB/s 且拒絕並行連線，官方權重 8.89 GB 需約
  3–4 小時。使用者選擇等待，不改用第三方產物。
- worker 只接受環境 allowlist、強制 `HF_HUB_OFFLINE`／`TRANSFORMERS_OFFLINE`、不繼承
  API key 與真實 `HOME`，且只從 host 驗證過的本機目錄載入，永不解析 repository ID。

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

## 完整管線與中央取消（M6，2026-08-28）

- 固定路徑為 `LocalAudioTransport.input → VoiceTurnGate → LuneFinalOnlySTTService →
  ContextEnricher → LLM provider → SentenceGate → TTSRouterService → PlaybackSink`，
  由 `build_voice_pipeline` 組裝，避免不同呼叫端各自接線。
- `GenerationCoordinator` 是唯一能推進 generation 的元件。取消時先同步遞增 ID 再拆除，
  順序固定為：可聽輸出 → TTS → STT → provider（Pipecat `InterruptionFrame` 並排空）→
  工具提案 → turn gate → transport。任一階段失敗都會記錄在 `CancelEvent.failed_stages`，
  不會讓後續階段被跳過。
- 200 ms 插話門檻量測的是「停止可聽輸出」這一段，因此它是取消流程的第一步，
  provider 排空與 transport 重建都排在量測之後。
- 插話是唯一允許音訊跨越 fence 的情況。turn gate 會保留進行中的 utterance、重新標記
  generation，並在重新標記後的 PCM 追上前，暫時接受被打斷 generation 的殘留資料。
  裝置切換、STT 逾時與輸出溢位一律丟棄，因為那些音訊不是使用者要說的下一句話。
- turn gate 在同一次呼叫內就把 VAD 事件解析成擷取結果，並在產生任何事件後立即讓出控制權，
  因此延遲事件無法擷取到已屬於新 generation 的音訊。輸入不連續（callback 被丟棄）會重建
  取樣時間軸並丟棄進行中的 utterance，而不是拼接錯位的音訊。
- 工作狀態（`thinking`、`speaking`）綁定其 generation。取消後狀態自動回到 idle，
  麥克風不會因為某個被取消的 turn 而卡在「AI 正在說話」的 300 ms 門檻。
- `PipecatAttemptProvider` 以 Pipecat frame 契約橋接任何 `LLMService`。取消透過廣播
  `InterruptionFrame` 完成，這正是 Responses WebSocket 送出 `response.cancel` 的路徑，
  因此 `remote_cancel` 的宣告仍然誠實。`LLMThoughtTextFrame` 不是 `LLMTextFrame`，
  推理內容不會經由這個對應進入 `SentenceGate` 或 TTS。
- provider 回報的 cache 明細若與 input token 總數不一致，一律往「較少快取」方向夾擠。
  這只會讓費用估得更高，不會低估。
- 端到端門檻維持 p50 ≤1.5 s、p95 ≤2.2 s，量測與評分放在 `lune.pipeline.benchmark`，
  證據不足時判定失敗。依既有實測拆解，此門檻在目標硬體上無法達成；是否縮短 Whisper
  輸入 window、更換模型或調整門檻，是尚未做出的產品決策，不得自行放寬。
- `AVSpeechSynthesizer` 的 buffer callback 只會送到**主執行緒**，而且只有主執行緒的
  `CFRunLoop` 被 drain 時才會送出（2026-08-28 目標機實測：主 run loop 有 drain 得到 271 個
  buffer，不 drain 為 0，run loop 放在其他執行緒也是 0）。因此 driver 使用
  `MainRunLoopPump`，在 asyncio 迴圈內以非阻塞方式 drain 主 run loop，沒有資料時自動放慢。
  位置仍由 `RunLoopHost` 抽換，M7 的 App 程序若已自行運轉主 run loop，可改注入自己的實作。
- AVSpeech 以爆發方式寫出整段 utterance，因此 adapter 的 bounded queue 必須容得下一整句。
  預設由 32 提高到 512（約十秒、約 0.5 MB）。背壓在此不可行：callback 是原生 push，
  阻塞主執行緒會拖垮整個程序，所以超出上限仍以失敗收場，而不是無界成長。

## M7 前半：最小實體語音垂直切片（2026-08-29）

- `lune.engine` 必須且只可呼叫既有 `build_voice_pipeline` 完成組裝；不得為硬體 smoke test
  另接一條簡化管線。production composition 固定使用本機 Silero、pinned MLX Whisper、E5
  context、OpenAI Responses provider contract、AVSpeech router 與同一個 playback fence。
- `CoreAudioStreamOwner` 同時擁有 PyAudio input stream 與實作既有 `AudioOutputDevice`。建立
  Python object 本身不初始化 PortAudio、不查裝置也不開 stream；實際 engine start 才以
  CoreAudio default device ID／transport type 和 PortAudio default index 建立記憶體內 snapshot。
- 冷啟動 input stream 關閉；只有明確要求 microphone 且 output 不是內建裝置時才打開。輸出
  stream 在第一個 PCM chunk 才 lazy open。CoreAudio default query 在前後 ID 不一致時拒絕該次
  snapshot，避免把切換中的 CoreAudio ID 與 PortAudio index 錯配。
- PyAudio input 使用 callback mode；callback 只驗證 int16 mono frame、複製進 bounded
  `LocalAudioTransport` 並立即回傳。callback overflow、status discontinuity、input／output
  stream error 都必須先由唯一 `GenerationCoordinator` 推進 fence，再重建 stream。
- PyAudio output 的 open／write／stop／close 都在單一 executor 序列化，避免 blocking call 阻塞
  engine 主執行緒上的 asyncio 與 AVSpeech `MainRunLoopPump`。每次實際 write 最多約 20 ms；
  `flush()` 在排入 executor 前先同步設中止旗標，使大 PCM chunk 也能在當前短區塊後停止。
- `PlaybackSink` 遇到 device write error 後，該 generation 的 `drain()` 必須回傳 false 並拒絕
  後續 chunk。未成功送達 output device 的 assistant 文字不得被當作已播放而落庫。
- 預設裝置以 bounded polling 監看。變更仍走既有 `DeviceStateMachine`：先取消 generation，
  再重建，內建輸出保持 `paused_unsafe_output`；裝置 UID 與名稱只留在記憶體，不進診斷。
- 公開 gate 只能注入 fake CoreAudio／PortAudio、deterministic VAD／STT、scripted provider、
  fake TTS 與 recording output。這些證據不代表麥克風權限、channel mapping、200 ms 插話、
  實體裝置切換或無殘留資源 gate 已通過；所有實體測試仍須另行逐項授權。

## 測試階段的 LLM 組成（2026-08-29 由使用者決定）

- 測試階段的 release 預設 provider 改為**本機 `Qwen3.5-4B` Q4**，雲端 `openai_responses`
  暫不啟用。理由是本階段要驗證的是實體語音路徑本身：本機 provider 不需要 API key、不產生
  費用、也不依賴網路，能讓 smoke test 與 soak 在完全離線的條件下重複執行。
- 這不是推翻 `docs/ui-spec.md` 已定案的 hybrid，而是**暫時把主備關係倒置**：測試階段本機為
  唯一 provider，雲端延後規劃；雲端就緒後恢復 ui-spec 的「OpenAI 為主、本機為備援」。
- 主力模型的首句延遲門檻（由端到端 p50 1.5 s 反推得到的 1,150 ms）在本階段**明示豁免**，
  理由與 ui-spec 的備援論證相同：離線可對話優於不可對話。門檻數值不得修改也不得刪除，
  spike 的失敗證據原樣保留。恢復雲端為主時，該門檻自動重新適用。
- 豁免只涵蓋 LLM 首句延遲。音訊路徑的 200 ms 插話門檻與端到端 p50／p95 維持原值；實體
  smoke test 仍以 scripted provider 量測，避免模型抖動污染音訊數據。
- 本機 provider 沿用既有 spike 的信任邊界：獨立 `mlx-lm` worker、環境 allowlist、強制離線、
  永不解析 repository ID，只從已 pin 且逐檔 checksum 通過的本機目錄載入。thinking 內容一律
  在 host 端濾除，不進 `SentenceGate`、TTS、記憶、SQLite 或診斷。
- 本機 attempt 的預留與結算成本為零，但仍走同一個 `BudgetLedger` 路徑，使雲端恢復後帳務
  語意不需要改寫。本機模式下不會出現 `budget_locked`。

## M7 第二階段：實體 smoke 期間確立的政策（2026-08-29）

- `PlaybackSink` 的預設容量由 32 提高到 512，理由與 M6 把 AVSpeech adapter queue 提高到 512
  完全相同：AVSpeech 以爆發方式送出整句 PCM（實測一句 2 秒中文 172 個 chunk、22,050 Hz），
  而 `_speak` 逐句 drain，因此佇列必須容得下一整句。背壓在此仍不可行。單句超過約六秒的語音
  會超出這個上限並以失敗收場，而不是無界成長。
- **input overflow 不再一律取消 generation**（2026-08-29 由使用者決定）。原政策使慢推論自我
  毀滅：Whisper 推論餓死 PyAudio callback，PortAudio 設下 `paInputOverflow`，於是取消掉推論
  正在餵養的那個 generation，答案永遠播不出來。新規則是：只有在 turn gate 有進行中的
  utterance 時才推進 fence；否則只重建取樣時間軸（與取消路徑相同的 transport rebuild），
  答案繼續產生。「不拼接錯位音訊」的原意由時間軸重建維持。
- PortAudio 輸出寫入改為 `exception_on_underflow=False`。underflow 表示裝置消耗得比寫入端快，
  是抖動而非裝置故障；把它當成致命錯誤會在回答中途拆掉輸出串流並取消 turn（實測一次情境
  出現 3 次輸出失敗、2 次 `stream_error`）。真實裝置錯誤仍然由 `stream.write` 拋出。
- **端到端時鐘改由擷取時間推導。** `speech_end_at` 原本取「擷取當下的 `monotonic()` 減句尾
  靜音」，等於把管線落後量算進端到端的起點，只會讓數字變好看（實測輸入落後 241.8 ms、
  峰值 595 ms）。`LocalAudioTransport` 現在為每個 callback 記錄取樣錨點，`VoiceSession` 以
  `wall_time_of_sample(last_voiced_sample)` 取得真正的說話結束時刻。這是讓實作符合計畫既有
  的端到端定義，不是門檻變更。
- Whisper 解碼溫度固定為 `(0.0, 0.2)`，即只保留一次重試（2026-08-29 由使用者決定）。upstream
  預設的六趟重試在真實收音上實測 6,605–10,271 ms，會撞破 10 秒 STT watchdog；單次解碼為
  1,842–1,889 ms。此設定會改變尚未執行的 M2 正規化準確率 gate 的條件，屆時須一併重測。
- `lune-engine` 新增 `--microphone` 與 `--ephemeral-memory` 兩個旗標。前者是 UI 出現前唯一能
  開始對話的方式（冷啟動 mic-off 的政策不變，開麥克風仍是明確請求）；後者讓 shakedown 對話
  不寫入沒有 bulk delete 的真實逐字稿、記憶與好感度。兩者都不改變預設行為。
- 延遲數字必須連同當時的電源模式與系統負載一起引用。實測低電量模式會讓 Whisper 暖狀態從
  1,826–1,889 ms 變成 6,676–7,290 ms，與程式無關。

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
- GPT-SoVITS runtime 固定於 upstream commit
  `48b1a0169a28582a8984402f82cf438d3bfa6aca`；runtime 與 voice manifest 各自保存於
  Application Support，repo 不提供 downloader。worker 與 host 啟動時都核對 revision，host
  每次啟動另重新執行 sandbox capability probe。
- Worker protocol version 1 使用 4-byte big-endian length prefix；control frame 為 bounded
  JSON，PCM frame 為 bounded binary signed 16-bit mono payload，並以 generation／sequence
  拒絕 late 或錯序資料。Stdout 只允許 protocol，upstream print 轉到由 host 丟棄的 stderr。
- `TTSRouterService` 以完整 utterance 選擇後端。GPT 在第一個 PCM 前失敗才可由 AVSpeech
  從頭 fallback；已輸出 PCM 後禁止中途換聲線。soft cancel 逾 500 ms 才終止目前已驗證的
  worker PID，重建失敗則 session circuit breaker 開啟。
- Apple 已將 `sandbox-exec` 標為 deprecated；因此可執行檔存在不等於可用，active denial
  probe 或 production profile 套用失敗都必須 fail closed。私人效能 gate 未通過前，即使設定
  偏好 GPT，release factory 仍固定選 AVSpeech。
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
