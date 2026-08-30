---
name: verify-change
description: 在 Lune repo 完成一組變更後，依後果與證據新鮮度挑選要跑的 gate，並用正確的證據等級陳述結論。改完程式或文件、準備回報「這樣就好了」之前使用。
---

# verify-change

目的是**用最少的 gate 取得足夠的證據**，並讓最後那句結論的強度與證據相符。
不是「把所有測試再跑一次」。

## 1. 判斷後果類別

用 `AGENTS.md` 的〈驗收矩陣〉對照本次變更範圍。同一次變更可能同時落在多列，取聯集。

## 2. 判斷證據新鮮度

對矩陣列出的每一項 gate，先問它現在是哪一種：

| 狀態 | 意義 | 動作 |
|---|---|---|
| FRESH | 本次變更之後跑過，且涵蓋被改動的面向 | 不重跑，引用既有結果 |
| STALE | 跑過，但之後又改了會影響它的東西 | 重跑 |
| MISSING | 沒跑過，或從來沒有涵蓋這個面向 | 跑，或明說沒有這項證據 |

只跑 STALE 與 MISSING。**不要為了安心重跑沒被影響的昂貴 gate**；也不要因為「上次綠的」
就跳過真的變髒的那一項。

## 3. 確定性缺陷先補回歸測試

可穩定重現的 bug：先寫一個**現在會失敗**的測試，確認它失敗，再修，再確認它通過。
沒有回歸測試的修正只能標 `CODE-ONLY`。

## 4. 執行

由近而遠：targeted pytest → 完整 pytest → lint／format／type → secret scan →
import／self-test → `git diff --check`。實際指令與結果都要留下，能被使用者原樣重跑。

先跑最接近變更的那一項：

```sh
uv run pytest tests/<最接近的目錄>/<最接近的檔案>.py
```

完整公開 gate 的**權威清單在 `AGENTS.md` 的〈完整公開 gate〉**，不要在這裡另抄一份。只改文件時可以只跑
`uv run python scripts/check_docs.py`，完整 `pytest` 也涵蓋它。

`import lune` 失敗先跑 `chflags -R nohidden .venv`，不要改 `pyproject.toml`。

引用測試數量前，先確認工作樹沒有 macOS 產生的重複檔（檔名帶 ` 2` 的複本會被 pytest 一起收集）：

```sh
uv run pytest -q --ignore-glob='* 2.py'
```

## 5. 標註結論

每一句宣稱都要配一個等級：`VERIFIED`／`CODE-ONLY`／`MANUAL-VERIFICATION-REQUIRED`／`BLOCKED`。

不可以做的事：

- 用 deterministic fake、mock 或公開 CI 的綠燈，宣稱硬體、私人模型、私人語料或雲端行為正確。
- 從程式碼推論延遲、記憶體或執行緒行為；那些只能量。
- 引用延遲數字卻不附當時的電源模式與系統負載。
- 為了讓 gate 變綠而放寬門檻、刪測試或加 skip。

## 完成條件

- 該跑的 STALE／MISSING gate 都跑完，指令與結果可重現。
- 每一項宣稱都有證據等級。
- 未執行與未通過的項目明確列出；沒有也要寫「無」。
