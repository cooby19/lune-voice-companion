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
| M5 | 完成（公開 gate） | 27 項 M5 tests／226 項完整 pytest；typed utterance／PCM 契約、bounded length-prefix protocol、固定 GPT-SoVITS revision、Python 3.10 isolated worker、AVSpeech PCM callback、generation／sequence fence、500 ms cancel、worker crash／sandbox denial、整句 fallback 與 session circuit breaker；[commit `bd59740`](https://github.com/cooby19/lune-voice-companion/commit/bd59740e293e39aa7226ce49fadd4e85163afbbf)；[CI #33073392282](https://github.com/cooby19/lune-voice-companion/actions/runs/33073392282) |
| M6 前置：本地 LLM spike | 進行中（公開 gate 完成，硬體 gate 未執行） | 102 項 spike tests／328 項完整 pytest；串流 `<think>` 濾除與違規記錄、pin 未建立即 fail-closed 的模型 manifest、loopback-only endpoint 政策、runtime 候選成本表、由端到端預算推導的首句延遲門檻、RSS／swap／queue 累積偵測、工具呼叫 schema 與每 turn 限額、取消證據與 `remote_cancel` 誠實標示、淨化報告；未安裝 runtime、未下載模型、未讀取私人 persona；[commit `c0a348e`](https://github.com/cooby19/lune-voice-companion/commit/c0a348ee14142b8a292240fa729c8659b09d0941)；[CI #33093928857](https://github.com/cooby19/lune-voice-companion/actions/runs/33093928857) |
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
- 硬體與私人模型報告只在本機產生；除非先完成淨化，否則不進版控。
- 每個里程碑必須先通過該階段 gate、更新本文件、建立可回退 commit 並 push，才進入下一階段。

## 後續交接

M2、M3、M4、M5 與本地 LLM spike 的 public gate 均已通過。M2 local model／私人語料、
M3 私人人格 rubric、M4 真實 E5、M5 私人 GPT 模型／效能 gate，以及本地 LLM spike 的
runtime、模型與硬體 gate 均尚未執行。後續工作請先閱讀
[`handoff-m2-m8.md`](handoff-m2-m8.md)。下一步需要使用者授權安裝 runtime 與下載 Q4 產物，
之後才能執行延遲、記憶體、取消與工具呼叫 gate，並據此提出 M6 provider 決策。
