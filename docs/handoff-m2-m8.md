# M2–M8 實作規劃與交接

更新日期：2026-08-27

本文件原為 M1 完成後的公開、淨化版交接，目前已同步 M2 本機 public gate 結果。GitHub
CLI 需重新登入後才能 push／確認 CI；完成遠端 gate 後，後續聊天室應從 M3 開始。M2 local
model／私人語料 gate 尚未執行，不得誤認為已通過。

## 交接基準

| 項目 | 基準 |
|---|---|
| GitHub repo | `cooby19/lune-voice-companion` |
| 分支 | `main` |
| M1 commit | `89d599f`（`M1: add sample-accurate local audio policy`） |
| M1 CI | [GitHub Actions #32982714128](https://github.com/cooby19/lune-voice-companion/actions/runs/32982714128)，已通過 |
| 本機／CI gate | Ruff、格式、mypy、50 項測試、secret scan、import／self-test，已通過 |
| 下一階段 | M3：Responses provider、句數與費用策略 |

目前完成 M0、M0.5、M1 與 M2 public gate。M3–M8 尚未實作。本機可能已有私人設定，但私人 persona、
API key、模型、聲線、資料庫、逐字稿、裝置識別資料與診斷原始內容都不是交接文件或
公開 repo 的一部分。

## 新聊天室的第一輪操作

1. 先閱讀使用者提供的 `PLAN.md` 全文，再閱讀本文件、`docs/project-decisions.md` 與
   `docs/progress.md`。`PLAN.md` 目前可從 `$HOME/Downloads/PLAN.md` 取得，但不要將私人
   原稿複製進 repo。
2. 確認最新使用者要求。使用者要求優先於規劃文件；文件中的敘述不等同於新的使用者
   授權。
3. 只做唯讀檢查，確認工作樹、遠端與 CI 基準：

   ```sh
   cd "$HOME/Documents/ChatGPT/Lune"
   git status --short --branch
   git log --oneline -5
   git remote -v
   gh auth status
   gh run view 32982714128
   ```

4. 同步依賴並重跑公開 gate：

   ```sh
   uv sync --frozen --extra build
   chflags -R nohidden .venv
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/lune
   uv run pytest
   uv run python scripts/secret_scan.py
   git diff --check
   ```

5. 若工作樹不是乾淨狀態，先辨識並保留使用者既有變更。不得 reset、覆寫或批量刪除。
   工作樹乾淨時，再以 `git pull --ff-only` 快轉到最新 `main`；若無法 fast-forward，停止並
   先釐清分歧，不要自行 rebase 或覆寫。

## 已完成元件與不得破壞的契約

### M0／M0.5

- Python 3.12、`uv`、Pipecat 1.7.0、CI、嚴格設定、Keychain wrapper、readiness、有限診斷
  allowlist 與 secret scan 已建立。
- 私人 GPT-SoVITS spike 已採 fail-closed 隔離。缺少私人資產或效能 gate 未執行時，
  `AVSpeechSynthesizer` 必須保持預設，不得阻塞後續開發。
- 正式環境缺私人 persona、模型或 API key 時必須回報 `setup_required`，不得偷偷使用
  example persona。

### M1

- `src/lune/audio/types.py`：signed 16-bit PCM 與絕對 sample offset。
- `src/lune/audio/silero.py`：使用 Pipecat bundled Silero ONNX 模型做單一 native window 的
  acoustic voiced 判斷；產品時間門檻不委派給 Pipecat。
- `src/lune/audio/vad.py`：以 sample count 實作 100／300／350 ms turn policy。
- `src/lune/audio/preroll.py`：700 ms ring，涵蓋 350 ms pre-roll 與最長 300 ms 確認窗。
- `src/lune/audio/transport.py`：麥克風預設關閉、bounded callback queue、overflow health、
  generation 與無舊 PCM 回流的 rebuild。
- `src/lune/audio/devices.py`：預設裝置變更必須先取消 generation，再重建；內建輸出保持
  `paused_unsafe_output`。

Pipecat 1.7.0 的 Silero native window 在 16 kHz 為 512 samples（32 ms），其內建秒數會
量化門檻，因此 Lune 必須繼續由 `TurnPolicy` 擁有精確 sample 門檻。不要改回 Pipecat 的
內建 100／300／350 ms timing。

目前 M1 只通過公開 mock／bundled-model self-test。安靜語音、背景音樂／風扇／鍵盤、
實體預設裝置切換及耳機驗收尚未執行，不得宣稱硬體 gate 已通過。真正的 PyAudio／
CoreAudio stream owner 仍須在 M6／M7 整合。

## 共通實作規則

- 每次只完成一個里程碑：先寫契約與 deterministic tests，再接真實 backend。
- 所有 STT、LLM、tool call 與 PCM 事件都必須攜帶 `generation_id`；舊 generation 永遠
  不得回到下游。
- 含逐字稿、prompt、PCM 或私人路徑的 dataclass／frame payload 欄位應設為
  `repr=False` 或等價保護，避免 assertion、例外與 debug repr 意外洩漏內容。
- 診斷不得包含逐字稿、prompt、persona、記憶、API key、私人路徑或裝置 UID。
- 公開 CI 不使用 API key、不下載大型模型，也不執行私人聲線或硬體 gate。
- 硬體／模型報告只能寫入 `~/Library/Logs/Lune/`，權限設為 `0600`；公開版只可提交已
  淨化的彙總。
- 文件使用繁體中文、ATX 標題、統一表格與 fenced code block；套件名、狀態與命令使用
  backticks。`LICENSE` 保留標準 MIT 英文原文。
- 禁止批量刪除檔案或目錄，也禁止 `git clean -fd`、`rm -rf` 或 destructive reset。
  若舊 build 產物必須批量清除，停止並請使用者手動處理。
- 每個里程碑完成時依序執行 targeted gate、完整公開 gate、secret scan、更新
  `docs/progress.md`、建立單一可回退 commit、push，最後等待 GitHub Actions 通過。

## M2：MLX Whisper final-only

狀態：本機 public gate 已完成，待 push／CI。已固定 upstream revision 與
`config.json`／`weights.npz` SHA-256，
實作 final-only typed event、四層 generation fence、容量 1 的 latest-wins pending、lazy
optional import 與 bounded `close()`。中文／英文／中英混流私人語料與本機模型效能 gate
尚未執行。

### 目標

將已完成的 PCM turn 交給本機 `mlx-community/whisper-large-v3-turbo-q4`，只輸出 final
transcript；取消或裝置切換後，即使同步推論稍後才結束，也不得輸出舊結果或舊錯誤。

### 建議檔案與契約

- `src/lune/stt/contracts.py`
  - `TranscriptionRequest`：`request_id`、`generation_id`、`AudioSpan`、可選
    `language_hint`。
  - `FinalTranscript`：保留 request／generation 關聯，並以型別固定為 final；不要建立
    interim transcript 對外契約。
  - `STTFailure`：只包含有限錯誤碼與 request／generation，不包含逐字稿或私人路徑。
- `src/lune/stt/model_manifest.py`
  - model ID 固定為 `mlx-community/whisper-large-v3-turbo-q4`。
  - revision 必須是可驗證的 40 字元 immutable commit hash，不可接受 `main`、tag 或
    浮動 alias。
  - 目前尚未選定並驗證實際 revision 與逐檔 checksum。M2 應從模型來源核對後記錄，
    不得猜測、沿用快取 metadata 或在測試中偽裝成已 pin。
  - 逐檔驗證相對路徑、regular file、no symlink 與 SHA-256；沿用 TTS spike 的 fail-closed
    manifest 思路。
  - 預設 manifest 位於 `LunePaths.whisper_manifest`。
- `src/lune/stt/mlx.py`
  - 基礎安裝與 CI 不應 import `mlx_whisper`；只在真實 inference 首次使用時 lazy import。
  - 將同步推論放入 `asyncio.to_thread()` 或等價 executor，避免阻塞事件迴圈。
  - 使用 bounded、latest-wins pending queue；正在跑的同步推論可自然結束，但結果必須經
    generation fence 後丟棄。
  - inference function 必須可注入，讓 CI 使用 deterministic fake，不下載模型。
  - 真實 inference 只能取得 manifest 驗證過的本機模型目錄。禁止把 Hugging Face repo ID
    直接交給 `mlx-whisper` 觸發隱式下載，也禁止在執行時解析 moving revision；缺檔時應
    fail closed 並回報 `setup_required`。

不要直接依賴 Pipecat 的 `pipecat.services.whisper.stt.WhisperSTTServiceMLX`。在目前固定的
Pipecat 1.7.0 中，該 module 會先 import 未安裝的 `faster_whisper`；其 segmented handler
也會 await 同步推論，使插話取消被阻塞。應以 Lune 自己的薄 adapter 隔離這些行為。

### Generation fence

至少在以下位置檢查目前 generation：

1. 接受／排入 request 前。
2. worker 真正開始 inference 前。
3. inference 回傳或拋錯後。
4. 將 final event 放入下游 queue 或 callback 前。

generation 變更時只做 epoch invalidation，不假裝能終止已進入 native code 的 thread。
bounded queue 中尚未開始的舊 request 應移除；同 generation 的 latest-wins 行為要有明確
測試與文件。

### Public gate

- gen 7 推論被 barrier 阻擋，切到 gen 8 後釋放；gen 7 結果與錯誤皆不得輸出。
- gen 8 final 必須恰好輸出一次，且沒有 interim event。
- worker 執行時事件迴圈仍可處理取消。
- pending queue 不得無界成長。`close()` 後不得再接受 request 或發出 event，也不得留下
  Lune 管理的 asyncio worker；已進入 native code 的 thread 只能以 generation fence 隔離，
  應記錄 bounded shutdown 限制並讓它自然結束，不可宣稱能強制取消。
- manifest 覆蓋缺檔、錯 hash、浮動 revision、`../`、symlink、非 regular file 與成功案例。
- module import 不需要 `mlx-whisper` optional extra；真實 backend 缺少 optional dependency 時
  回傳有限 `setup_required`／錯誤碼，不洩漏路徑。
- 全專案 lint、format、mypy、pytest、secret scan 通過。

### Local model／語料 gate

此 gate 不在公開 CI 執行。安裝 `mlx` extra 並放置 pinned model 後，使用已獲授權的本機
語料測試中文、英文、中英混流各至少 10 句；正規化準確率至少 85%，並記錄
speech-end → final 的 p50／p95／max。報告不得包含音訊、逐字稿或私人模型路徑。模型與
語料不存在時，只能標示「未執行」，不得以 mock gate 取代。

### 完成條件

- 更新 `docs/progress.md`，明確分列 public gate 與 local model gate。
- 建議 commit：`M2: add generation-fenced final-only STT`。
- push 後等待 CI 成功，才進入 M3。

## M3：Responses provider、句數與費用

### 實作順序

1. 建立 `ProviderCapabilities`、`LLMProviderFactory` 與 deterministic fake provider；pipeline
   只依賴 typed Pipecat frames。
2. 以 Pipecat 1.7.0 的 `OpenAIResponsesLLMService` 實作 `openai_responses` registry entry，
   使用 Responses WebSocket、`store=False`，不要改用 Realtime API。
3. Terra 與 Luna 使用兩個獨立 service instance。預設 Terra：`reasoning.effort="none"`、
   最多約 192 output tokens、standard service tier；開始實作時再次以官方 OpenAI 文件
   核對模型與參數。
4. 實作中英文完整句 boundary 的 `SentenceGate`，最多放行三句；第三句完成後只向上游
   取消剩餘生成。使用者插話則由中央 coordinator 做全管線 interruption。
5. 第一句尚未送入播放前遇到一次暫時錯誤，才可重試 Luna。已有音訊播放後不得重播另一
   份答案，只播放本機短錯誤提示。
6. 建立 Asia/Taipei 月界線的 usage reservation／ledger：預設匯率 33.0；預留後達
   NT$700 改用 Luna，達 NT$900 則 `budget_locked`，禁止送出新雲端 request。

判斷門檻時使用「本月已確認實際成本＋所有 active reservations＋本次 worst-case
reservation」；Terra 與可能的 Luna retry 各自建立 attempt 並分別計費。傳往 OpenAI 的
context 只能包含精簡 summary、必要近期 turns 與最多五筆相關記憶，並關閉 tracing／
telemetry。

### 已知 Pipecat 1.7.0 風險

- `OpenAIResponsesLLMService` 的 import 路徑為
  `pipecat.services.openai.responses.llm`；設定與 reasoning 型別應使用套件公開 alias。
- interruption 會送 `response.cancel` 並 drain，但 drain 期間的 terminal usage 可能不會被
  service 留給應用層。取消／error／incomplete 且無 usage 時，ledger 必須使用完整預留額。
- `MetricsFrame` 是 `SystemFrame`，可能超越一般 frame；usage 必須另帶本機 `attempt_id`，
  不得只靠 queue 順序配對。
- Pipecat DEBUG／TRACE 可能輸出完整 persona 與 messages；正式環境只能使用 INFO 以上，
  並以 Lune allowlist diagnostics 包裝。

### Gate

- fake provider 覆蓋成功、第一次暫時錯誤、播放後錯誤、remote cancel、late token、late tool
  call、usage 缺失與 metrics reorder。
- 100 組中英文 punctuation fixture 驗證最多三句且不截斷第三句。
- 700／900 邊界以預留後金額判斷；月份、匯率、取消估算與重試費用皆有測試。
- 私人 persona 從 Application Support 載入；12 題 rubric 至少 10 題合格，但題目與回答
  不得進公開 repo 或診斷。

## M4：SQLite 記憶與關係狀態

### 實作重點

- 以 migration 建立 `sessions`、`turns`、`messages`、`summaries`、
  `long_term_memories`、`relationship_state`、`relationship_events`、`llm_usage`。
- 每個 connection 啟用 foreign keys、WAL、busy timeout 與 `secure_delete`；資料庫與目錄
  使用私人權限。
- 最近 12 個完整 turns 進 prompt；第 13 個完成後，以 Luna 將最舊四個併入連續、
  不重疊 coverage 的 rolling summary。
- pin `intfloat/multilingual-e5-small` 的 `model.safetensors` revision／checksum；384 維、
  `query:`／`passage:` 前綴、本機 cosine full scan、top-k 5、門檻 0.72、最多回填 1,200 字。
- `propose_memory`／`propose_affinity` 只能提案；host 驗證、去重與限額後才提交。affinity
  初始 50、全域 0–100、單 turn ±1、單 session ±3。
- final user transcript 才可保存；assistant 只保存實際確認播放部分。取消的文字、摘要與
  工具提案不得落庫。
- CLI 只提供逐筆 `forget <exact-id>`，二次確認後硬刪單筆；不得提供 bulk clear。

### Gate

Migration 冪等、重啟召回、summary coverage、重複 proposal、不完整 turn、取消不落庫、
affinity audit、exact-ID deletion，以及 10 個黃金檢索至少 8 個進 top-k。

## M5：TTS 正式 backend

### 實作重點

- 固定公開契約：`TTSRequest`、`PCMChunk`、`StreamingTTSBackend.synthesize()`／`cancel()`／
  `close()`；每個事件帶 `generation_id`。
- `TTSRouterService` 以完整 utterance 選 backend，不在一句中途切換聲線。
- 正式化 GPT-SoVITS Python 3.10 worker、長度前綴 stdin/stdout protocol 與 stderr-only log；
  沿用 M0.5 已通過 mock gate 的 validator／sandbox capability 與 fail-closed 決策。私人
  manifest、模型推論與效能尚未驗證，不得把 spike 結果當成正式 backend gate。
- worker 只接收 environment allowlist，不得繼承 API key；禁止網路，只讀 pinned runtime／
  voice 路徑並使用專用 temp。每次合成使用可拋棄 child PID；每次 backend 啟動都重驗
  sandbox capability，套用失敗即 fail closed。
- checkpoint／pickle 即使 checksum 正確仍可能執行惡意程式碼；正式文件與 UI 必須保留
  這項殘餘風險，不得把 hash 描述成安全證明。
- soft cancel 500 ms 後只可終止已驗證的明確 worker PID；重建再失敗則本 session 關閉
  GPT backend。任何失敗都保留 AVSpeech fallback。
- 公開 repo 不得加入 downloader、私人 checkpoint、參考音訊或參考文字。

### Gate

協定分片／錯序／超長 frame、generation cancellation、worker crash、sandbox denial、circuit
breaker 與 AVSpeech fallback。私人 GPT gate 需 TTFA p95 ≤1.0 s、RTF p95 ≤0.8、peak RSS
≤6 GB、15 分鐘無 serious／critical thermal，並在取消後 500 ms 內停止；未通過即維持
AVSpeech 預設。

## M6：中央取消與完整管線

### 實作重點

- 建立唯一的 `GenerationCoordinator`：遞增 generation、發 interruption、作廢 STT、取消並
  drain Responses、清 TTS／裝置 queue、撤銷未提交工具提案。
- 組裝固定路徑：

  ```text
  LocalAudioTransport.input
    → LuneFinalOnlySTTService（自訂 MLX adapter）
    → LLMContextAggregator
    → ContextEnricher
    → OpenAIResponsesLLMService
    → SentenceGate
    → TTSRouterService
    → LocalAudioTransport.output
  ```

- 對 `TurnPolicy` 與 `PreRollBuffer` 明確 reset／generation fence。目前 M1 API 不接收
  expected generation；不得讓延遲 VAD event 以相同 sample 座標擷取新 generation 音訊。
- queue 必須 bounded；錯誤與取消路徑都要結束 task、釋放 frame 並保持可重新聆聽。

### Gate

- 打斷確認後 200 ms 內停止可聽輸出，之後沒有舊 token、tool call 或 PCM。
- 30 個暖機 turns：最後 voiced sample → 第一個非靜音 output frame，p50 ≤1.5 s、
  p95 ≤2.2 s。
- 覆蓋 STT 卡住、LLM late event、TTS worker crash、queue overflow、裝置切換與重試；未通過
  release gate 時預設 AVSpeech，不得隱藏失敗。

## M7：選單列 App、IPC 與打包

### 實作重點

- rumps UI 與 `lune-engine` 分離；engine 綁 `127.0.0.1:0`，以一次性 JSON handshake 回傳
  port、protocol version 與隨機 token，後續只接受 authenticated WebSocket。
- 命令：`set_microphone`、`get_status`、`shutdown`；事件：`state_changed`、
  `device_changed`、`budget_changed`、`error`。
- 冷啟動 mic off；內建喇叭保持 `paused_unsafe_output`；預設裝置 UID 只留在記憶體，不能
  寫入公開 log。
- Quit 先 graceful shutdown；逾時只處理 handshake 驗證過的 engine／worker PID。
- py2app 薄打包，不把 Whisper、E5、GPT runtime 或私人聲線放進 `.app`。
- 在 app metadata 加入 `NSMicrophoneUsageDescription`，並驗證首次權限提示；不要等到 M8
  才補，否則 M7 的 Finder 啟動 gate 無法成立。

### Gate

無 token／錯 token／重放 handshake、engine crash/reconnect、裝置切換、Finder 雙擊、
首次麥克風權限、mic-off cold start、內建輸出暫停，以及退出後 3 秒內無 child process。

## M8：安全、soak 與 release

### 實作重點

- API key 只透過安全輸入寫入 macOS Keychain；不得出現在 CLI argument、config、環境、
  log、trace 或 crash report。
- 驗證 M7 已加入的 `NSMicrophoneUsageDescription`，完成 ad-hoc codesign 與 release
  self-test；MVP 不公證、不做 login item。
- 完成 privacy scan、固定依賴檢查、README 設定與已知限制；明列 OpenAI 文字資料
  邊界、SQLite 未額外加密與 AVSpeech fallback。
- 驗證每個診斷檔本身為 `0600`，不能只依賴 `SafeDiagnostics` 建立的 `0700` 目錄權限。
- 執行 30 分鐘 soak：queue 不單調累積、RSS 不持續上升、無 serious／critical thermal，
  最後 10 分鐘 p95 不比最初 10 分鐘惡化 25% 以上。

### 完成條件

公開 CI、硬體 gate、模型 gate、30 分鐘 soak 與 secret scan 都有明確結果；任何未執行或
未通過項目都列入已知限制，不得以「應該可用」取代驗證。

## 建議的下一個聊天室提示

```text
先完整閱讀我提供的 PLAN.md，以及 repo 的 docs/handoff-m2-m8.md、
docs/progress.md、docs/project-decisions.md。先確認 M2 commit 已 push 且 CI 通過，再以最新
main 為基準只實作 M3：Responses provider、三句 gate、取消 drain 與 700／900 費用策略。
先查官方 OpenAI 與 Pipecat 1.7.0 文件，保留所有私人資料邊界；完成 targeted／full gate、
更新進度、建立單一 commit、推送並確認 CI 後停止回報。不得批量刪除任何檔案或目錄。
```
