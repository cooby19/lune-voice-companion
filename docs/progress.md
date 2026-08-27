# 開發進度

更新日期：2026-08-28

| 里程碑 | 狀態 | Gate 證據 |
|---|---|---|
| M0 | 完成 | uv／Python 3.12、CI、隱私邊界、繁中文件、12 項測試、secret scan、公開 repo／`main` |
| M0.5 | 完成 | 17 項 spike 測試；程式化 file／network denial probe 通過；缺私人資產時固定 AVSpeech |
| M1 | 完成（公開 gate） | 21 項測試；Silero bundled-model self-test；99／100、100／299／300／301、349／350 ms sample gate；700 ms pre-roll；bounded queue、重建競態與裝置狀態機；[CI #32982714128](https://github.com/cooby19/lune-voice-companion/actions/runs/32982714128) |
| M2 | 完成（公開 gate） | 17 項 M2 測試；immutable revision／逐檔 SHA-256；final-only typed event；四層 generation fence；bounded latest-wins pending；lazy optional import 與 bounded close；[commit `ebe262d`](https://github.com/cooby19/lune-voice-companion/commit/ebe262d1fc588351d1b2598d23cfaa9eb48dca8e)；[CI #32990678422](https://github.com/cooby19/lune-voice-companion/actions/runs/32990678422) |
| M3 | 完成（公開 gate） | 119 項 M3 tests／186 項完整 pytest；Pipecat Responses WebSocket registry；Terra／Luna 獨立 instance；三句 cancel/drain、retry／late-event 與 700／900 ledger；[commit `3d1e084`](https://github.com/cooby19/lune-voice-companion/commit/3d1e084633f025bb51084b3ec3abcc61f82fd753)；[CI #33033271278](https://github.com/cooby19/lune-voice-companion/actions/runs/33033271278) |
| M4 | 完成（公開 gate） | 13 項 M4 tests／199 項完整 pytest；8-table migration、private SQLite pragmas／permissions、13th-turn rolling summary、E5 384 維 bounded retrieval、proposal dedupe／cancel、affinity audit、usage 重啟還原與 exact-ID CLI；[commit `a5ef5f0`](https://github.com/cooby19/lune-voice-companion/commit/a5ef5f0d36f29f53e34eb360604e90ee2177ff24)；[CI #33071346597](https://github.com/cooby19/lune-voice-companion/actions/runs/33071346597) |
| M5 | 完成（公開 gate） | 27 項 M5 tests／226 項完整 pytest；typed utterance／PCM 契約、bounded length-prefix protocol、固定 GPT-SoVITS revision、Python 3.10 isolated worker、AVSpeech PCM callback、generation／sequence fence、500 ms cancel、worker crash／sandbox denial、整句 fallback 與 session circuit breaker；[commit `bd59740`](https://github.com/cooby19/lune-voice-companion/commit/bd59740e293e39aa7226ce49fadd4e85163afbbf)；[CI #33073392282](https://github.com/cooby19/lune-voice-companion/actions/runs/33073392282)；2026-08-28 修復 AVSpeech 原生 driver 的 CFRunLoop 缺口，完整 pytest 366 → 372 項 |
| M6 前置：本地 LLM spike | 完成（gate 已執行，效能未通過） | 102 項 spike tests／328 項完整 pytest；串流 `<think>` 濾除與違規記錄、pin 未建立即 fail-closed 的模型 manifest、loopback-only endpoint 政策、runtime 候選成本表、由端到端預算推導的首句延遲門檻、RSS／swap／queue 累積偵測、工具呼叫 schema 與每 turn 限額、取消證據與 `remote_cancel` 誠實標示、淨化報告；未安裝 runtime、未下載模型、未讀取私人 persona；[commit `c0a348e`](https://github.com/cooby19/lune-voice-companion/commit/c0a348ee14142b8a292240fa729c8659b09d0941)；[CI #33093928857](https://github.com/cooby19/lune-voice-companion/actions/runs/33093928857) |
| M6 | 待處理 | 完整 pipeline 與插話 benchmark |
| M7 | 待處理 | 選單列 App、authenticated IPC 與打包 |
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
- AVSpeech 原生 driver 的 CFRunLoop 缺口已於 2026-08-28 修復。`writeUtterance_toBufferCallback_`
  的 buffer 一律送到**主執行緒**的 run loop，因此改由擁有主執行緒的 asyncio event loop 週期
  抽取（`_MainRunLoopPump`，5 ms tick、每 tick 最多抽 4 次），並且每抽一次只往下游送一個
  合併後的 chunk，讓有界佇列不被離線 render 的速度衝爆。M5 的既有測試注入 fake driver，
  無法涵蓋這條路徑，因此新增 `tests/tts/test_avspeech_native.py`：三項 `integration` 測試
  直接驅動真實 driver，在 AVFoundation 或語音不可用時自動 skip；另三項單元測試涵蓋 pump
  的有界抽取與停止語意。完整 pytest 由 366 項增為 372 項。
- 該修復的實測值：修復前，等第一個 chunk 的測試在 10.13 秒的 bounded timeout 後失敗；修復後
  0.62 秒通過。經 `AVSpeechAdapter` 全路徑量測 20 句中／英短句（含暖機），TTFA p50 24 ms、
  p95 179 ms、max 179 ms，短句每句 1～3 個 chunk；合成期間 10 ms 目標的心跳實測 p50 12.6 ms、
  max 19.5 ms，event loop 維持可回應。這些是本機真實 AVSpeech 量測，不是 mock 結果；私人
  GPT 的效能 gate 仍未執行。
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
  是否改採 hybrid、維持 OpenAI 或退到更小尺寸是新的產品決策，尚未決定，不得自行降低門檻
  或靜默切換。
- 硬體與私人模型報告只在本機產生；除非先完成淨化，否則不進版控。
- 每個里程碑必須先通過該階段 gate、更新本文件、建立可回退 commit 並 push，才進入下一階段。

## 後續交接

M2、M3、M4、M5 的 public gate 與本地 LLM spike 的完整 gate（含實機延遲、記憶體、取消、
工具呼叫與私人 persona rubric）均已執行完畢。M2 local model／私人語料、M4 真實 E5、
M5 私人 GPT 模型／效能 gate 仍未執行。

M6／M7 接上 AVSpeech 時必須維持兩個前提：engine 的 asyncio loop 跑在主執行緒
（`src/lune/engine.py` 的 `asyncio.run` 已符合），且維持逐句合成——有界佇列「溢位即
`synthesis_failed`」的語意未改，而 AVSpeech 的 render 遠快於即時播放，單一 utterance
超過 32 個 chunk 接上即時 sink 時仍會溢位。細節見 `docs/handoff-m2-m8.md`。

本地 LLM spike 的結論是：`Qwen3.5-4B` Q4 在此硬體上行為正確、資源充裕，但首句延遲無法
滿足既有端到端門檻。M6 的 LLM provider 因此仍是 `openai_responses`，等待使用者就
hybrid、維持 OpenAI 或改用更小模型做出新的產品決策後，才能固定 M6 pipeline 組成。
