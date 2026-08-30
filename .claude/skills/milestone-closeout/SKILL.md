---
name: milestone-closeout
description: 在 Lune repo 收尾一個里程碑或一組相關缺陷：界定完整的關閉集合、搜尋同類問題、同步 docs/progress.md 與相關文件、建立可回退 commit 並等 GitHub Actions。要「把這件事做完」而不只是修掉眼前那一個時使用。
---

# milestone-closeout

這個 repo 的歷史是一連串「里程碑 → 全綠 → 同步文件 → 單一可回退 commit → 等 CI」。
這個流程就是那件事，重點在**不要在第一個修好的缺陷就收工**。

## 1. 界定關閉集合

先寫下這次要關掉的完整集合是什麼（哪些行為、哪些檔案、哪些 gate），再開始實作。
集合沒被耗盡之前不算完成。「一個 commit 只做一件事」不等於「第一個 commit 之後就停」。

## 2. 先探索再實作

在動手前把相關面全部找出來：受影響的模組、對應測試、對應文件段落、對應的既有決策。
探索結果維護成一份清單（哪些已處理、哪些待處理、哪些明確排除且為什麼）。

## 3. 一個缺陷確認後，搜尋同類

**這是這個 repo 最常見的漏網方式。** 確認一個失敗類別之後，主動找同一類的其他位置。
真實例子：M6 把 AVSpeech adapter 的 bounded queue 從 32 提到 512，但下游 `PlaybackSink`
仍是 32，直到 M7 的實體 smoke 才發現每一輪都被 `output_overflow` 取消。凡是接在 AVSpeech
後面的 bounded queue 都屬同一類。

同類搜尋的典型軸線：同一個常數、同一個契約、同一個 fence 檢查點、同一種錯誤處理策略。

## 4. 每批變更後驗收

用 `verify-change` 挑 gate，不要每改一行就跑全套，也不要全部改完才第一次跑。
可安全修復且在範圍內的相關缺陷，在同一次收尾裡一起修，不要只登記不處理。

## 5. 同步文件

全綠之後、commit 之前：

- `docs/progress.md` **必更**：本次的 gate 證據、通過／未通過／未執行分開列。
- 行為、決策或操作方式有變時，同步 `README.md`、`docs/project-decisions.md`、
  `docs/ui-spec.md`、`docs/handoff-m2-m8.md` 中受影響的段落。
- 引用程式碼時寫路徑加符號名稱，不要寫行號。
- 跑 `uv run python scripts/check_docs.py`。

## 6. commit 與 CI

- 任一 public gate 未綠時**不得** commit 或 push。
- 建立**單一可回退 commit**，訊息用祈使句描述行為改變（沿用既有風格，如
  `M7: carry retrieved memory ids to the message they answered`）。
- push 之後等 GitHub Actions，把 run 連結記進 `docs/progress.md`。
- 不 force push、不 rebase、不 destructive reset。

## 7. 回報

明列：完成了什麼、關閉集合是否耗盡（未耗盡就列出剩下什麼）、跑了哪些 gate 與結果、
證據等級、風險、未執行的驗收。沒有也要寫「無」。

## 停止條件

關閉集合耗盡、公開 gate 全綠、文件同步、commit 已 push 且 CI 通過。
然後停下來，不要順手開始下一件功能。
