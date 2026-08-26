# 開發進度

更新日期：2026-08-26

| 里程碑 | 狀態 | Gate 證據 |
|---|---|---|
| M0 | 完成 | uv／Python 3.12、CI、隱私邊界、繁中文件、12 tests、secret scan、公開 repo／`main` |
| M0.5 | 完成 | 17 個 spike tests；程式化 file／network denial probe 通過；缺私人資產時固定 AVSpeech |
| M1 | 完成（公開 gate） | 21 tests；Silero bundled-model self-test；99／100、100／299／300／301、349／350 ms sample gate；700 ms pre-roll；bounded queue、重建競態與裝置狀態機 |
| M2 | 待處理 | MLX Whisper final-only 與 generation 作廢 |
| M3 | 待處理 | Responses provider、三句 gate、取消與費用策略 |
| M4 | 待處理 | SQLite 記憶、檢索、工具、affinity 與 CLI |
| M5 | 待處理 | TTS protocol、worker 隔離與 AVSpeech fallback |
| M6 | 待處理 | 完整 pipeline 與插話 benchmark |
| M7 | 待處理 | 選單列 App、authenticated IPC 與打包 |
| M8 | 待處理 | Keychain、簽署、soak／隱私／release gate |

## 驗收原則

- Mock 測試結果不得冒充硬體 gate。
- M1 的安靜語音、環境噪音與實體裝置切換 gate 尚待在目標 Mac 與耳機上執行；目前只完成可公開重現的程式與 mock gate。
- 硬體與私人模型報告只在本機產生；除非先完成淨化，否則不進版控。
- 每個里程碑必須先通過該階段 gate、更新本文件、建立可回退 commit 並 push，才進入下一階段。
