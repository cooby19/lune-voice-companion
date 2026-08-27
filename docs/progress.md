# 開發進度

更新日期：2026-08-27

| 里程碑 | 狀態 | Gate 證據 |
|---|---|---|
| M0 | 完成 | uv／Python 3.12、CI、隱私邊界、繁中文件、12 項測試、secret scan、公開 repo／`main` |
| M0.5 | 完成 | 17 項 spike 測試；程式化 file／network denial probe 通過；缺私人資產時固定 AVSpeech |
| M1 | 完成（公開 gate） | 21 項測試；Silero bundled-model self-test；99／100、100／299／300／301、349／350 ms sample gate；700 ms pre-roll；bounded queue、重建競態與裝置狀態機；[CI #32982714128](https://github.com/cooby19/lune-voice-companion/actions/runs/32982714128) |
| M2 | 完成（公開 gate） | 17 項 M2 測試；immutable revision／逐檔 SHA-256；final-only typed event；四層 generation fence；bounded latest-wins pending；lazy optional import 與 bounded close；[commit `ebe262d`](https://github.com/cooby19/lune-voice-companion/commit/ebe262d1fc588351d1b2598d23cfaa9eb48dca8e)；[CI #32990678422](https://github.com/cooby19/lune-voice-companion/actions/runs/32990678422) |
| M3 | 完成（本機公開 gate） | 119 項 M3 tests／186 項完整 pytest；Pipecat Responses WebSocket registry；Terra／Luna 獨立 instance；`store=false`、`reasoning.effort=none`、192 output tokens、default tier；100 組中英文標點 fixture；三句 cancel/drain、generation／attempt fence、重試與 late-event；Asia/Taipei 700／900 reservation ledger 與 `2026-07-30` 價格版本 |
| M4 | 待處理 | SQLite 記憶、檢索、工具、affinity 與 CLI |
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
  Asia/Taipei 月界線；將 usage 寫入 SQLite 並在重啟後還原 confirmed cost 屬於 M4。
- 硬體與私人模型報告只在本機產生；除非先完成淨化，否則不進版控。
- 每個里程碑必須先通過該階段 gate、更新本文件、建立可回退 commit 並 push，才進入下一階段。

## 後續交接

M2 public gate 與 GitHub Actions 已通過；M3 public gate 已在本機完成，等待 commit、push 與
GitHub Actions 證據。M2 local model／私人語料與 M3 私人人格 rubric 尚未執行。後續工作請先
閱讀 [`handoff-m2-m8.md`](handoff-m2-m8.md)，並在 M3 remote gate 通過後從 M4 開始。
