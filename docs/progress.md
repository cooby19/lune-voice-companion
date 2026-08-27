# 開發進度

更新日期：2026-08-27

| 里程碑 | 狀態 | Gate 證據 |
|---|---|---|
| M0 | 完成 | uv／Python 3.12、CI、隱私邊界、繁中文件、12 項測試、secret scan、公開 repo／`main` |
| M0.5 | 完成 | 17 項 spike 測試；程式化 file／network denial probe 通過；缺私人資產時固定 AVSpeech |
| M1 | 完成（公開 gate） | 21 項測試；Silero bundled-model self-test；99／100、100／299／300／301、349／350 ms sample gate；700 ms pre-roll；bounded queue、重建競態與裝置狀態機；[CI #32982714128](https://github.com/cooby19/lune-voice-companion/actions/runs/32982714128) |
| M2 | 完成（公開 gate） | 17 項 M2 測試；immutable revision／逐檔 SHA-256；final-only typed event；四層 generation fence；bounded latest-wins pending；lazy optional import 與 bounded close；[commit `ebe262d`](https://github.com/cooby19/lune-voice-companion/commit/ebe262d1fc588351d1b2598d23cfaa9eb48dca8e)；[CI #32990678422](https://github.com/cooby19/lune-voice-companion/actions/runs/32990678422) |
| M3 | 完成（公開 gate） | 119 項 M3 tests／186 項完整 pytest；Pipecat Responses WebSocket registry；Terra／Luna 獨立 instance；三句 cancel/drain、retry／late-event 與 700／900 ledger；[commit `3d1e084`](https://github.com/cooby19/lune-voice-companion/commit/3d1e084633f025bb51084b3ec3abcc61f82fd753)；[CI #33033271278](https://github.com/cooby19/lune-voice-companion/actions/runs/33033271278) |
| M4 | 完成（公開 gate；remote 待驗證） | 13 項 M4 tests／199 項完整 pytest；8-table migration、private SQLite pragmas／permissions、13th-turn rolling summary、E5 384 維 bounded retrieval、proposal dedupe／cancel、affinity audit、usage 重啟還原與 exact-ID CLI |
| M5 | 待處理 | TTS protocol、worker 隔離與 AVSpeech fallback |
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
- 硬體與私人模型報告只在本機產生；除非先完成淨化，否則不進版控。
- 每個里程碑必須先通過該階段 gate、更新本文件、建立可回退 commit 並 push，才進入下一階段。

## 後續交接

M2 與 M3 public／remote gate 已通過；M4 public gate 已通過，remote gate 待本次 commit push
後驗證。M2 local model／私人語料、M3 私人人格 rubric 與 M4 真實 E5 模型 gate 尚未執行。
後續工作請先閱讀 [`handoff-m2-m8.md`](handoff-m2-m8.md)，並從 M5 開始。
