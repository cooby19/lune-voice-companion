# 開發進度

更新日期：2026-08-30

| 里程碑 | 狀態 | Gate 證據 |
|---|---|---|
| M0 | 完成 | uv／Python 3.12、CI、隱私邊界、繁中文件、12 項測試、secret scan、公開 repo／`main` |
| M0.5 | 完成 | 17 項 spike 測試；程式化 file／network denial probe 通過；缺私人資產時固定 AVSpeech |
| M1 | 完成（公開 gate） | 21 項測試；Silero bundled-model self-test；99／100、100／299／300／301、349／350 ms sample gate；700 ms pre-roll；bounded queue、重建競態與裝置狀態機；[CI #32982714128](https://github.com/cooby19/lune-voice-companion/actions/runs/32982714128) |
| M2 | 完成（公開 gate） | 17 項 M2 測試；immutable revision／逐檔 SHA-256；final-only typed event；四層 generation fence；bounded latest-wins pending；lazy optional import 與 bounded close；[commit `ebe262d`](https://github.com/cooby19/lune-voice-companion/commit/ebe262d1fc588351d1b2598d23cfaa9eb48dca8e)；[CI #32990678422](https://github.com/cooby19/lune-voice-companion/actions/runs/32990678422) |
| M3 | 完成（公開 gate） | 119 項 M3 tests／186 項完整 pytest；Pipecat Responses WebSocket registry；Terra／Luna 獨立 instance；三句 cancel/drain、retry／late-event 與 700／900 ledger；[commit `3d1e084`](https://github.com/cooby19/lune-voice-companion/commit/3d1e084633f025bb51084b3ec3abcc61f82fd753)；[CI #33033271278](https://github.com/cooby19/lune-voice-companion/actions/runs/33033271278) |
| M4 | 完成（公開 gate） | 13 項 M4 tests／199 項完整 pytest；8-table migration、private SQLite pragmas／permissions、13th-turn rolling summary、E5 384 維 bounded retrieval、proposal dedupe／cancel、affinity audit、usage 重啟還原與 exact-ID CLI；[commit `a5ef5f0`](https://github.com/cooby19/lune-voice-companion/commit/a5ef5f0d36f29f53e34eb360604e90ee2177ff24)；[CI #33071346597](https://github.com/cooby19/lune-voice-companion/actions/runs/33071346597) |
| M5 | 完成（公開 gate） | 27 項 M5 tests／226 項完整 pytest；typed utterance／PCM 契約、bounded length-prefix protocol、固定 GPT-SoVITS revision、Python 3.10 isolated worker、AVSpeech PCM callback、generation／sequence fence、500 ms cancel、worker crash／sandbox denial、整句 fallback 與 session circuit breaker；[commit `bd59740`](https://github.com/cooby19/lune-voice-companion/commit/bd59740e293e39aa7226ce49fadd4e85163afbbf)；[CI #33073392282](https://github.com/cooby19/lune-voice-companion/actions/runs/33073392282) |
| M6 前置：本地 LLM spike | 完成（gate 已執行，效能未通過） | 102 項 spike tests／328 項完整 pytest；串流 `<think>` 濾除與違規記錄、pin 未建立即 fail-closed 的模型 manifest、loopback-only endpoint 政策、runtime 候選成本表、由端到端預算推導的首句延遲門檻、RSS／swap／queue 累積偵測、工具呼叫 schema 與每 turn 限額、取消證據與 `remote_cancel` 誠實標示、淨化報告；未安裝 runtime、未下載模型、未讀取私人 persona；[commit `c0a348e`](https://github.com/cooby19/lune-voice-companion/commit/c0a348ee14142b8a292240fa729c8659b09d0941)；[CI #33093928857](https://github.com/cooby19/lune-voice-companion/actions/runs/33093928857) |
| M6 | 完成（公開 gate）；端到端 benchmark 未執行 | 81 項新測試／447 項完整 pytest；唯一 `GenerationCoordinator`、barge-in carry-over turn gate、Pipecat provider bridge、bounded playback fence、STT watchdog、工具提案兩階段提交與 benchmark gate 邏輯；30 輪暖機端到端 benchmark 與實體音訊 gate 未執行；[commit `de561f8`](https://github.com/cooby19/lune-voice-companion/commit/de561f8b848c64f3b3f11d162cf6c3c5a0ae3466)；[CI #33179142684](https://github.com/cooby19/lune-voice-companion/actions/runs/33179142684)；AVSpeech run loop 修正 [commit `ad353e6`](https://github.com/cooby19/lune-voice-companion/commit/ad353e6)；[CI #33185129924](https://github.com/cooby19/lune-voice-companion/actions/runs/33185129924) |
| M7 前半：最小實體語音垂直切片 | 完成（公開 gate）；實體 gate 待授權 | 真正的 PyAudio／CoreAudio input owner 與 `AudioOutputDevice`、engine 唯一管線組裝、mic-off／unsafe-output policy、裝置監看與完整關閉；56 項 targeted tests／459 項完整 pytest；未開啟裝置、未讀取私人資料、未載入本機模型、未發出雲端請求 |
| M7 第三階段：本機 LLM provider | 完成（公開 gate）；實體 gate 未執行 | `local_qwen` 進入 release registry 並成為測試階段預設；Pipecat `LLMService` 包住既有隔離 worker，串流 token、`<think>` 濾除、`<tool_call>` 抽取、零價 ledger 記帳與 pinned primary；19 項新測試／478 項完整 pytest；未載入權重、未開啟裝置、未發出雲端請求 |
| M7 第二階段：無雲端實體 smoke | 部分執行 | 已授權並在目標 Mac 執行：冷啟動 mic-off、耳機辨識、麥克風開啟、**一次完整實體 turn**（端到端 2,664.7 ms，門檻 p50 ≤1.5 s 未通過）、關閉後無殘留；插話 200 ms 與裝置切換未執行；期間修正五項缺陷；486 項完整 pytest |
| M7 後半：桌面殼與 authenticated IPC | 部分完成（公開 gate）；實體與打包 gate 未執行 | loopback WebSocket IPC（一次性 token、protocol 版本、訊息上限、單一 client）、`lune-engine --ui-ipc` 引擎子行程與一行私人 handoff、`UiRuntime` 命令與 snapshot 契約、pywebview 視窗殼與內嵌 Web UI；519 項完整 pytest；rumps 依 `docs/ui-spec.md` 放棄，py2app 只更新設定、實際打包與簽署未執行 |
| M7 後半補完：增量 UI 事件通道 | 完成（公開 gate） | `message_added`／`thread_updated`／`memory_updated` 由 `MemoryStore` 的 commit 後通知接上發送端，payload 與 snapshot 共用同一份 per-item view；snapshot 退為 2 秒一次、只在改變時送出的對帳，送事件會推進對帳基準；事件佇列有上限，滿了丟事件而非阻塞引擎，並強制下一次完整對帳；`app.js` 補上 `memory_updated` 分支、修正 thread 排序與通話計時被事件倒退；25 項新測試／550 項完整 pytest |
| 訊息上的記憶標記接上資料鏈 | 完成（公開 gate） | `turn_retrieved_memories`（schema v3，兩個外鍵皆 `ON DELETE CASCADE`）；enricher 保留檢索排名、turn 提交時寫入、`_message_view` 的 `memory_ids` 由 snapshot 與 `message_added` 共用；`app.js` 補上套用命令回傳 snapshot 的分支，硬刪記憶當下即無殘影；11 項新測試／587 項完整 pytest |
| M8 | 待處理 | Keychain、簽署、soak／隱私／release gate |

## 驗收原則

- Mock 測試結果不得冒充硬體 gate。
- M1 的安靜語音、環境噪音與實體裝置切換 gate 尚待在目標 Mac 與耳機上執行；目前只完成可公開重現的程式與 mock gate。
- M2 的中文、英文、中英混流各 10 句、正規化準確率與 speech-end → final 延遲 gate 尚未執行；
  目前沒有讀取私人模型或語料，也沒有以 deterministic fake 冒充 local model gate。
- M3 的 public gate 不使用 API key，也不發出 OpenAI 請求。私人 persona 12 題 rubric 尚未獲准
  讀取或執行，因此標示為「未執行」；deterministic fake 不冒充人格品質驗收。
- M3 ledger 已實作當月 confirmed cost、active reservation、取消保守估算、retry 分次計費與
  Asia/Taipei 月界線；M4 已將 settled usage 寫入 SQLite，並在重啟後還原 confirmed cost。
- M4 公開 gate 使用 deterministic 384 維 encoder 驗證 10／10 golden query、`query:`／
  `passage:` 契約與 bounded cosine full scan，不下載模型。實際 E5 模型檔尚未放置，因此本機
  真實模型載入與語意品質驗收標示為「未執行」，沒有以 fake 結果冒充。
- M5 公開 gate 使用 deterministic fake worker 與 fake AVSpeech callback；未讀取私人 voice
  manifest、checkpoint、參考音訊或參考文字，也未啟用實體音訊輸出。私人 GPT 的固定中／英／
  混流 corpus、TTFA p95、RTF p95、peak RSS、15 分鐘 thermal 與真實取消 gate 均標示為
  「未執行」，因此 release 預設仍為 AVSpeech。
- 本地 LLM spike 已完成可公開重現的 harness 與 gate 邏輯，但尚未改動 provider registry、
  OpenAI readiness、費用策略或 M6 pipeline。`lune.llm_spike` 目前不安裝任何 runtime、
  不下載模型、不讀取私人 persona，也沒有註冊本地 provider 名稱；`decide_local_provider`
  在缺任一證據時固定回傳 `openai_responses`。
- spike 的公開 gate 只驗證 harness 本身：`<think>` 串流濾除（含跨 chunk 分割標籤）、
  manifest fail-closed、endpoint 只允許 loopback、gate 在證據缺失時判定失敗而非通過。
  首 token／首句延遲、30 輪穩定性、peak RSS、memory pressure／swap、thermal、真實取消與
  真實工具呼叫全部標示為「未執行」，deterministic 測試不冒充硬體結果。
- 首句延遲門檻刻意不獨立設定，而是由既有端到端 p50 ≤1.5 s 扣除 350 ms 句尾靜音、
  M2 的 Whisper final 延遲與 M5 的 TTS TTFA 推導。M2／M5 的本機 gate 尚未執行，因此
  目前推導結果為「未定」，且未定會使 performance gate 判定失敗，不會意外通過。
- Q4 產物來源尚未決定，因此模型 pin 仍為未建立狀態。官方 `Qwen/Qwen3.5-4B` 本身沒有官方
  Q4 版本，採用第三方轉換或自行量化是尚待授權的選擇；在 pin 建立前任何 manifest 檢查都
  fail closed。第一候選已於 2026-08-28 由 9B 改為 4B，是為了先用較低成本判斷本地路徑
  可行性，不代表 4B 已通過任何 gate。
- 本地 LLM spike 的實機 gate 已於 2026-08-28 在目標 MacBook Air M4／16GB 執行完畢，
  使用本機量化的 `Qwen3.5-4B` Q4（4.503 bits／weight，2.2 GB，視覺塔已於轉換時丟棄）。
  行為類 gate 全數通過，效能 gate 未通過：

  | 項目 | 實測 | 門檻 | 判定 |
  |---|---|---|---|
  | 冷啟動／暖啟動 | 2,680／2,602 ms | 記錄用 | 記錄 |
  | 首 token p50／p95 | 614／752 ms | 記錄用 | 記錄 |
  | 首句 p50 | 1,337 ms | ≤1,150 ms | 未通過 |
  | 首句 p95 | 3,855 ms | 見下 | 未通過 |
  | 生成速度 p50 | 19.8 tokens/s | 記錄用 | 記錄 |
  | peak RSS | 3.05 GB | ≤10 GiB | 通過 |
  | memory pressure／thermal | `normal`／`nominal` | 不得 warn／serious | 通過 |
  | RSS／swap／queue 累積 | 0.24%／0%／無 | 不得單調累積 | 通過 |
  | 30 輪穩定性 | 30 輪無失敗 | ≥30 | 通過 |
  | 取消 | 5 次試驗無 late token／tool call | 零 late 事件 | 通過 |
  | 工具呼叫 | schema、限額、去重皆符合 | — | 通過 |
  | non-thinking | 42 次回覆零推理外洩 | 零違規 | 通過 |
  | 私人 persona rubric | 10／12 | ≥10 | 通過 |

- 首句 1,150 ms 上限的推導：端到端 p50 ≤1.5 s 扣除 350 ms 句尾靜音後的**全部**餘裕，
  等於假設 Whisper 與 TTS 皆為 0 ms。實測首句 p50 為 1,337 ms，已超出這個不可能達成的
  寬鬆上限 187 ms；p95 3,855 ms 為上限的 3.35 倍。因此結論不依賴尚未執行的 M2／M5
  本機延遲測量，也不會因上游改善而翻案。
- 瓶頸在生成速度而非首 token：首 token p50 僅 614 ms，但 19.8 tokens/s 使一個完整中文
  句子仍需約 700 ms 以上。記憶體與散熱有大量餘裕（3.05 GB／16 GB，全程 `nominal`），
  所以失敗原因是延遲，不是資源。
- `remote_cancel` 未宣告：取消 gate 通過（無 late 事件、停止在期限內），但部分試驗中生成
  在 cancel 抵達前就自然結束，因此無法在每次試驗都證明停止本機推論。依規定不得宣告。
- persona rubric 未通過的兩題為自動關鍵字判準未命中，尚未經人工複核，不等於模型確實
  違規；rubric 題目與回覆依規定不進公開 repo 或診斷。
- 依 `project-decisions.md`，4B Q4 未通過延遲 gate 即視為本地即時路徑在此硬體上不成立。
  該產品決策已於 2026-08-29 由使用者做出：`docs/ui-spec.md` 定案為 hybrid，而測試階段先以
  本機為唯一 provider、雲端延後。首句延遲門檻是**明示豁免**而非降低，數值與本節的失敗
  證據都原樣保留。
- M6 的公開 gate 全部使用 deterministic fake：fake VAD 分類器、fake STT、scripted provider、
  scripted TTS backend 與 recording output device。沒有開啟麥克風或輸出裝置，沒有載入 Whisper、
  E5 或任何 LLM 權重，也沒有發出雲端請求。
- M6 已驗證的行為：fence 在任何拆除動作之前就同步推進；可聽輸出在其他階段之前停止，且
  `CancelEvent.audible_stop_ms` 就是 200 ms 門檻量測的值；插話後不再有舊 generation 的 PCM 到達
  輸出裝置；被取消的 turn 不寫入逐字稿、assistant 內容、記憶或 affinity；插話語音會成為下一個
  utterance 並帶著 pre-roll 送進 STT；STT 卡住由 watchdog 取消並保持可重新聆聽；輸出佇列溢位
  取消該次生成；裝置切換取消並在內建喇叭時暫停。
- M6 的 **30 輪暖機端到端 benchmark 未執行**：它需要實體麥克風、已放置的 Whisper 與 E5 模型、
  私人 persona 與 API key。`lune.pipeline.benchmark` 只提供量測與評分邏輯，並在證據不足時判定
  失敗，不會因缺資料而通過。
- 依 handoff 已記錄的實測拆解（句尾靜音 350 ms + Whisper final p50 約 1,959 ms + LLM 首句
  + TTS TTFA），端到端 p50 ≤1.5 s 在目標 MacBook Air M4 上不可能達成，且主因是 Whisper 的
  固定 30 秒 mel window，不是 LLM 選擇。M6 因此保留原門檻與失敗判定，release 預設維持
  AVSpeech；是否縮短 Whisper window、更換模型或調整門檻仍是未決的產品決策。
- M6 修正 M5 的 AVSpeech run loop 缺口，並已於 2026-08-28 在目標 Mac 實測（僅合成到記憶體，
  未開啟麥克風或輸出裝置，未讀取任何私人資產）：

  | 情境 | buffer 數 | 首個 buffer |
  |---|---|---|
  | 主執行緒 run loop 有 drain | 271 | 300 ms |
  | 由 worker thread 呼叫、主 run loop 有 drain | 271 | 140 ms |
  | 完全不 drain run loop | 0 | 無 |
  | run loop 放在專用 worker thread | 0 | 無 |

  結論：callback 一律送到**主執行緒**，與呼叫 `writeUtterance:` 的執行緒無關；只有主執行緒的
  run loop 被 drain 時才會有資料。第一版實作把 run loop 放在專用執行緒是錯的，已改為
  `MainRunLoopPump`：在 asyncio 迴圈內以非阻塞方式 drain 主 run loop，閒置時自動放慢。
- 上述修正暴露另一個 M5 缺陷：AVSpeech 會把整段 utterance 以爆發方式寫出，一句約三秒的中文
  在 consumer 被排程前就送出 271 個 buffer，使原本 32 個 chunk 的 bounded queue 直接 overflow
  並回報 `synthesis_failed`。預設容量已改為 512（約十秒、約 0.5 MB），仍對異常長輸入 fail closed。
- 以 Lune 自己的 `AVSpeechAdapter` 實測（同樣只寫記憶體）：

  | 語料 | 冷啟動 TTFA | 暖啟動 TTFA | 音訊長度 |
  |---|---|---|---|
  | 中文 | 450 ms | 119 ms | 3,143 ms |
  | 英文 | 280 ms | 118 ms | 2,206 ms |
  | 中英混流 | 121 ms | 118 ms | 4,059 ms |

  取消在第一個 chunk 之後即停止。TTFA 高於 handoff 先前記錄的 p50 28 ms，因為那次量測是以緊迴圈
  直接 pump 主 run loop，而 release 路徑是 5 ms 間隔的 asyncio pump 加上排程延遲。
- M6 另修正一個既有隱私缺口：Pipecat 的 `TextFrame.__str__` 會印出 payload，使 M3 的
  `repr=False` 在 log、assertion 與例外訊息中失效。`GenerationLLMTextFrame` 現在自訂 `__str__`。
- M7 前半的公開 gate 新增真正的 `CoreAudioStreamOwner`，但所有 adapter tests 都注入 fake
  CoreAudio property reader 與 fake PortAudio host；engine integration 只使用 deterministic
  VAD／STT、scripted provider、fake TTS 與 recording output。這些測試沒有列舉或開啟實體裝置。
- 輸入採 PyAudio callback mode，callback 只複製 signed-16-bit PCM 到既有 bounded
  `LocalAudioTransport`；format／status discontinuity、callback queue overflow 或 stream error
  會經唯一 `GenerationCoordinator` 作廢 generation 後重建。冷啟動及內建輸出時 input stream
  維持關閉。
- 輸出實作既有 `AudioOutputDevice`，明確固定當下的 PortAudio default index，blocking lifecycle
  與 write 放在單一 executor，不阻塞主 asyncio／CFRunLoop。單次 device write 最多約 20 ms，
  `flush()` 會先同步設中止旗標，因此大 PCM chunk 不會讓舊輸出無界阻塞取消；實際 200 ms
  硬體 gate 尚未執行。
- `lune.engine` 現在只透過既有 `build_voice_pipeline` 組裝 transport → VAD → STT → E5 context →
  provider → TTS → output，並擁有 input pump、預設裝置監看、stream recovery 與依序 shutdown。
  AVSpeech 仍由主執行緒上的 `MainRunLoopPump` 驅動，沒有退回 worker-thread run loop。
- 真實 output write 失敗時，`PlaybackSink.drain()` 現在回傳失敗且拒絕同 generation 後續 PCM，
  避免把未播放的 assistant 內容誤寫入 SQLite。
- M7 前半的**實體硬體 gate 尚未執行**：麥克風權限、取樣率／channel mapping、一次完整對話、
  插話 200 ms、裝置拔除／切換、內建喇叭暫停與退出後無殘留資源，都等待使用者分項授權。
- M7 第三階段依 2026-08-29 的決策接上本機 provider：`LocalQwenLLMService` 把既有隔離 worker
  包成一般 Pipecat `LLMService`，因此 generation fence、三句 gate、兩階段工具提案與 cancel／drain
  都沿用原路徑，`build_voice_pipeline` 沒有為它另開分支。
- 本機組成沒有第二層可退，因此 primary 是釘死的：ledger 不會為只有一個 provider 的組成去
  選 Terra 或 Luna。本機 attempt 價格為零，且零價預留不受雲端鎖定阻擋——這正是 ui-spec 說的
  「本機備援使 `budget_locked` 從死路變成分岔」。
- `remote_cancel` 對 `local_qwen` 宣告為 false。host 仍會終止自己 spawn 的 PID，但 spike 無法在
  每次試驗都證明 cancel 先於自然結束抵達，因此不宣告。
- `check_readiness` 改為依 `models.provider` 判斷：本機組成不讀 Keychain，改為要求已 pin 的
  模型 manifest 與 worker runtime 存在。
- **本機 provider 的實體 gate 未執行**：載入真實權重、實測首句延遲、實測取消是否停住本機推論、
  以及與麥克風／輸出裝置串起來的端到端行為，都還沒跑過。上述證據全部來自公開 CI 級測試。
## M7 第二階段：實體 smoke 的實測（2026-08-29，目標 MacBook Air M4／16GB）

已於使用者分項授權後執行：麥克風、耳機／實體輸出、本機 Whisper／E5、本地 LLM，之後另行取得
私人 `config.toml`／`persona/kernel.yaml` 的讀取授權。全程離線、無雲端請求、無 API key。
證據只保留數值與狀態碼，沒有音訊、逐字稿或私人路徑。

### 已通過

| 項目 | 實測 |
|---|---|
| 冷啟動狀態 | `mic_off`，麥克風關閉，輸出非內建 |
| 麥克風開啟 | 16 kHz mono，`input_open=true`，狀態 `listening` |
| 一次完整 turn | `completed`，播出 1 句，`degraded_tts=false` |
| 輸出裝置 | 22,050 Hz mono（AVSpeech 原生取樣率） |
| 取消／溢位／stream 失敗 | 全部 0 |
| 關閉後 | 0 child process、0 殘留 asyncio task、input／output 皆關閉 |
| 真實 provider 啟動 | `lune-engine --microphone` 以 `local_qwen` 組成抵達 `listening`（載入權重、人格、Whisper、E5、AVSpeech） |

### 未通過與未執行

| 項目 | 門檻 | 實測 | 判定 |
|---|---|---|---|
| 端到端（最後 voiced sample → 第一個非靜音輸出） | p50 ≤1,500 ms | 2,664.7 ms | 未通過 |
| Whisper final（2,908 ms 音訊、單次解碼） | 記錄用 | 1,878.6 ms | 記錄 |
| 插話 200 ms | ≤200 ms | — | 未執行 |
| 裝置切換／拔除 | — | — | 未執行 |
| 30 輪暖機 benchmark | p50 ≤1.5 s、p95 ≤2.2 s | — | 未執行 |

端到端 2,664.7 ms 的組成為句尾靜音 350 ms + Whisper 1,878.6 ms + scripted LLM（約 0）+ AVSpeech
TTFA。與既有拆解一致：瓶頸是 Whisper 的固定 30 秒 mel window，不是 LLM。門檻與失敗證據原樣保留。

### 量測環境的影響（必須與數字一併引用）

低電量模式開啟且 load average 約 4.9 時，同一份合成語料、同一版 `mlx` 0.32.2／`mlx-whisper`
0.4.3，Whisper 暖狀態需 6,676–7,290 ms；關閉低電量模式並降到 load 2.45 後為 1,826–1,889 ms。
**3.6 倍差距完全來自機器狀態**，與 Lune 的程式無關。任何延遲數字都必須註明當時的電源模式與負載。

### 本階段修正的五項缺陷

1. `PlaybackSink` 容量 32 對上 AVSpeech 的爆發式輸出（實測一句 2 秒中文送出 172 個 chunk、
   22,050 Hz），release 預設語音路徑每一輪都以 `output_overflow` 被取消。已改為 512，與 M6
   對 adapter queue 的處置同一理由與同一數值。
2. `_run_turn` 未包住 provider 的非預期例外：turn task 靜默死亡、DB turn 停在 pending、
   沒有 report，session 卡在 `thinking`。現在收斂為 `provider_error` 結果並回到可聆聽狀態。
3. input overflow 一律取消 generation，使慢推論自我毀滅——推論餓死 PyAudio callback，
   overflow 取消掉推論正在餵養的那個 generation。已改為只有進行中的 utterance 才取消。
4. `exception_on_underflow=True` 把正常的輸出 underflow 當成致命錯誤（實測一次插話情境出現
   3 次 `output_stream_failures`、2 次 `stream_error`）。underflow 現在視為抖動，真實裝置錯誤
   仍然拋出。
5. 端到端時鐘由處理時間推導：`speech_end_at` 取「擷取當下的 `monotonic()` 減句尾靜音」，
   管線落後多少就把端到端低估多少（實測輸入落後 241.8 ms、峰值 595 ms；某次量得 1,525 ms
   小於同一輪的 STT 2,463 ms 而露餡）。已改為由 transport 的取樣時間錨點反推最後 voiced
   sample 的擷取時刻，符合計畫既有的端到端定義。

### Whisper 解碼重試（2026-08-29 由使用者決定）

真實麥克風收音的短片段常使 `avg_logprob` 落在 −1.7 至 −4.2，upstream 預設會沿
0.2→0.4→…→1.0 重試最多六趟：實測溫度 `0.0` 時延遲 1,842–1,889 ms，溫度 `1.0` 時
6,605–10,271 ms，直接撞破 10 秒 STT watchdog 並使下一句排隊。使用者選擇**只保留一次重試**
（`(0.0, 0.2)`）。此設定會影響尚未執行的 M2 正規化準確率 gate，屆時須一併重測。

- 硬體與私人模型報告只在本機產生；除非先完成淨化，否則不進版控。
- 每個里程碑必須先通過該階段 gate、更新本文件、建立可回退 commit 並 push，才進入下一階段。

## thread 自動標題接上管線（2026-08-30）

`docs/ui-spec.md` 的〈已定案的決策〉要求 thread 標題在第一輪結束後自動生成一次、可手動改名，
且測試階段由本機模型生成、不得為此另開一次雲端請求。在此之前只有 store 層的
`set_generated_conversation_title()` 存在，整個 `src/` 沒有任何呼叫端。本次把這條線接起來。

| 位置 | 角色 |
|---|---|
| `src/lune/memory/titles.py` | `ThreadTitleManager`，決定何時嘗試、如何清理輸出、何時放棄 |
| `src/lune/llm/titles.py` | `LocalQwenTitleBackend`，把該輪對話交給已載入的本機 worker |
| `src/lune/llm/local_qwen.py` | 新增 `complete_once()`，同一個 worker 的線外生成 |
| `src/lune/pipeline/session.py` | `name_thread_if_due()`，掛在 turn 完成路徑 |

### 觸發點與取消語意

觸發點放在 `_finish_turn()` 內 `outcome == "completed"` 之後、`summarize_if_due()` 之前，此時
狀態已經寫回 idle，所以標題生成不會把 session 留在 `thinking`，也不會讓麥克風繼續套用插話門檻。
它仍在 turn task 內被 await，關閉流程照原本方式取消它。

fence 用的是**賺到這一輪的 generation**，不是當下的 generation。取消一次會前進 fence，因此
在模型還在選字時插話，寫入前的第二次 fence 檢查就會擋掉——與取消的一輪不留逐字稿是同一個規則。
`ThreadTitleManager` 只在「標題仍是 default」且「恰好一輪 complete」時嘗試，所以是一次，
不是每輪一次；失敗與取消都不寫入，預設標題原樣保留，對話流程不受影響。

### 為什麼不是另開一次請求

本機只有一個 worker 行程、一次一個 generation。`complete_once()` 因此與 turn 路徑共用同一把
鎖、不推任何 pipeline frame（句子閘與 TTS 看不到它）、不帶 proposal tools；而且 turn 永遠優先
——新的 generation 進來會先取消背景生成再取用 worker，被取消的標題回傳空字串。測試階段的成本
因此是零，且整段標題文字沒有離開這台機器。

雲端組成刻意仍不帶 title backend（`_cloud_composition()` 回傳 `title_backend=None`）。最終形態
要由備援模型在自己的請求內順帶產生，在那之前寧可留著預設標題，也不偷買第二次雲端請求。

### UI 通道

標題寫入成功時 store 會發出既有的 `StoreChange("thread")`，`UiRuntime._change_events()` 把它變成
P3 已完成的 `thread_updated`，payload 與 snapshot 用同一份 `_thread_view`，因此不需要新事件。

### Gate

完整 pytest 576 項通過（新增 26 項），Ruff lint／format、mypy、secret scan、import／self-test、
`git diff --check` 全綠。**未執行**：沒有實際用 `Qwen3.5-4B` Q4 跑過一次真實標題生成，所以標題
品質、生成耗時，以及插話搶回 worker 的實機時序都尚未量測。

## 訊息上的記憶標記接上資料鏈（2026-08-30）

`src/lune/ui/static/app.js` 的 `renderMessage()` 早就會在 `message.memoryIds` 非空時畫出一顆
「來自她記得的事」，`normalizeMessage()` 也解析 `memory_ids`——但沒有任何一端送過這個欄位，
所以那顆按鈕從來沒出現過。斷點在 `ContextEnricher._memories()`：`retriever.search()` 回傳的
`MemorySearchResult` 帶著 id，`memory_contents()` 只取文字，id 當場丟棄。

本次把這條線接起來。做這件事的理由不是補完一顆按鈕，而是〈你與 Lune 的設定檔〉那頁對使用者
承諾「她自己注意到的 —— 聊到相關的事才會想起來」，而介面裡沒有任何一處能讓人驗證這句話。

| 位置 | 改動 |
|---|---|
| `src/lune/memory/migrations.py` | migration 3：`turn_retrieved_memories(turn_id, memory_id, position)` |
| `src/lune/memory/store.py` | `record_retrieved_memories()`；`StoredMessage.memory_ids`；`conversation_messages()` 單次查詢帶出關聯 |
| `src/lune/pipeline/enricher.py` | `enrich()` 改回傳 `EnrichedContext`（context + 檢索排名的 id） |
| `src/lune/pipeline/session.py` | id 掛在 `_ActiveTurn` 上，於 `complete_turn()` 之前寫入 |
| `src/lune/ui/runtime.py` | `_message_view` 增加 `memory_ids` |
| `src/lune/ui/static/app.js` | 套用命令回傳的整包 snapshot（見下） |

### 為什麼寫在提交時，而不是檢索當下

檢索發生在 `_generate()` 內，那時 turn 還可能被插話取消。取消的一輪不留逐字稿，就不該留下
「它讀過哪些記憶」的紀錄，所以 id 先掛在 `_ActiveTurn` 上，只有 `_commit_turn()` 會寫入，
而且寫在 `complete_turn()` 之前——這樣 store 發出的既有 `StoreChange("messages")` 轉成
`message_added` 時，payload 已經帶著 `memory_ids`，不需要新事件。

寫入失敗（例如使用者剛好在這兩者之間刪掉那筆記憶）一律吞掉：關聯是介面提示，turn 不能陪葬。
`record_retrieved_memories()` 用 `INSERT ... SELECT ... FROM long_term_memories WHERE id = ?`，
記憶不存在就是零列，不會撞外鍵。

### 刪除不留殘影

`forget_memory()` 是逐筆硬刪，`ON DELETE CASCADE` 讓關聯同時消失，所以資料庫層不可能殘留。
介面層原本還差一步：`forget_memory` 回傳的是一整包 snapshot，但 `app.js` 的 result 分支只認
`message.snapshot` 與 `get_status`，這份回傳被丟掉，標記要撐到下一次對帳 tick 才更新。
本次補上以外形辨識整包 snapshot 的分支，刪除當下即生效。這個缺陷在 `memory_ids` 之前不可見。

介面拿到的只有 id，記憶文字一律由記憶清單提供，因此已刪除的記憶不可能循訊息這條路回到畫面上。

### 未做與未定

- **不回填。** 過去的檢索沒有被記錄過，既有對話永遠不會有標記，第一次看到要等下一輪新對話。
- **點擊行為仍未定**（`docs/ui-spec.md`〈尚未決定〉）。目前點下去只切到記憶面板、不高亮。
  要做高亮就得連同記憶面板的 `_MEMORY_LIMIT = 16` 一起決定。
- **文案已改。** 原稿的「來自她記得的事」宣稱的是因果，實際只能保證「這幾筆進了那一輪的 prompt」，
  因此改為「她想起了一件事」。約束本身也寫進了 ui-spec。

### Gate

587 項完整 pytest 通過（新增 11 項），Ruff lint／format、mypy、secret scan、`git diff --check` 全綠。
`lune self-test` 與 `import py2app; import lune.app; import lune.engine` 需帶 `PYTHONPATH=src` 才通過：
`.venv` 內的 editable 安裝目前沒有生效，與本次變更無關（pytest 自己設定 `pythonpath = ["src"]`，
所以測試不受影響）。**未執行**：沒有真的開過視窗看這顆標記，前端沒有測試框架，
`app.js` 只能靠讀取實體檔案的契約測試守住。

## 後續交接

M2、M3、M4、M5、M6 的 public gate 與本地 LLM spike 的完整 gate（含實機延遲、記憶體、取消、
工具呼叫與私人 persona rubric）均已執行完畢。M2 local model／私人語料、M4 真實 E5、
M5 私人 GPT 模型／效能 gate，以及 M6 的 30 輪端到端 benchmark 與實體音訊 gate，仍未執行。
AVSpeech 的 run loop 需求已於 2026-08-28 實測完畢並據此修正實作。

本地 LLM spike 的結論是：`Qwen3.5-4B` Q4 在此硬體上行為正確、資源充裕，但首句延遲無法
滿足既有端到端門檻。`PipecatAttemptProvider` 以 Pipecat frame 契約為介面，換成別的 provider
不需要改動 pipeline。使用者已於 2026-08-29 做出決策：長期組成為 hybrid（`docs/ui-spec.md`），
測試階段則先以本機 `Qwen3.5-4B` Q4 為唯一 provider、雲端延後規劃，首句延遲門檻於該階段
明示豁免。詳見 `project-decisions.md` 的「測試階段的 LLM 組成」。

M7 前半已完成公開可重現的 engine／stream adapter 與唯一管線接線。下一步必須先取得四項獨立
授權，才可執行無雲端實體 smoke test：麥克風、耳機／實體輸出、本機 Whisper／E5、本地 LLM。
在該 gate 前不讀取私人設定、模型或裝置內容。

M7 後半已不再是待處理：**rumps 依 `docs/ui-spec.md` 放棄**，產品形態改為左側邊欄的視窗
應用程式，`setup.py` 的 `LSUIElement` 已改為 `False`。**authenticated IPC 已實作**——
`src/lune/ipc/` 是只綁 `127.0.0.1:0`、一次性 token、單一 client 的 loopback WebSocket server，
`lune-engine --ui-ipc`（`run_ui_ipc`）是它的引擎端 host，唯一的 stdout 輸出是給 pywebview 父
行程的一行 handshake；`src/lune/ui/` 則是 `UiRuntime` 命令／snapshot 契約、pywebview 殼與內嵌
Web UI。**py2app 仍未完成**：`setup.py` 已改成視窗應用程式的薄打包設定，但實際 `.app` 打包、
簽署與 bundle 內的路徑驗證都還沒執行，連同 Keychain 與 soak／隱私／release gate 一起留在 M8。

上述證據全部來自公開 CI 級測試（519 項完整 pytest，deterministic fake engine 與 loopback
socket）。**桌面殼的實機 gate 未執行**：沒有真的開過 pywebview 視窗、沒有量過殼與引擎的
啟動與關閉時序，也沒有驗證過視窗關閉後引擎子行程確實回收。
