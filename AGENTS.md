# AGENTS.md

## 可執行指令

### 設定或更新開發環境時

```sh
uv sync --frozen --extra build
chflags -R nohidden .venv
uv run lune self-test
```

### 撰寫或修改測試時

- 先跑最接近變更的測試，例如：

```sh
uv run pytest tests/audio/test_vad.py
uv run pytest tests/tts_spike/test_manifest.py
```

### 驗證程式碼變更時

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy src/lune
uv run pytest
uv run python scripts/secret_scan.py
uv run python -c "import py2app; import lune.app; import lune.engine"
uv run lune self-test
git diff --check
```

### 建置 macOS App 時

- 先確認 `build/` 與 `dist/` 不存在；若存在，停止並請使用者手動處理舊產物。

```sh
uv run python setup.py py2app
```

## 專案身分與非預期的技術選擇

- Lune 是 macOS 14+、Apple Silicon 的 local-first 選單列語音夥伴；核心使用 Python
  3.12、`uv`，語音管線整合固定於 Pipecat 1.7.0。
- Python 套件由 Hatchling 1.27.0 建置；`setup.py` 只供 py2app 0.28.10 打包 macOS App。
  這是刻意保留的雙軌配置，不要把 `setup.py` 當成可直接移除的舊檔。
- 實驗性 GPT-SoVITS worker 刻意使用獨立 Python 3.10 與 `sandbox-exec`；核心 runtime
  不與它合併。
- `~/Downloads/PLAN.md` 是仍需遵守的原始方向。它不是逐項現況證明；實作完成度以 repo
  內的 progress 與測試證據為準。

## 行為偏好與工作流

- 只修改目前任務需要的範圍。若本次修改會直接造成另一處 bug，可連同修正並說明因果；
  不做無關重構。
- 涉及第三方套件、OpenAI API 或 macOS 行為時，先查官方文件或原始碼，不只靠記憶。
  可從 [OpenAI Developers](https://developers.openai.com/)、
  [Pipecat Docs](https://docs.pipecat.ai/) 與
  [Apple Developer Documentation](https://developer.apple.com/documentation/) 開始。
- 技術選擇用簡單繁體中文解釋：說明要改什麼、原因、風險與替代方案，不假設使用者熟悉
  Python、打包工具或語音管線。
- 若 `PLAN.md` 與官方現況衝突，先列出衝突與建議決策，取得同意後才偏離原方向。
- 修正失敗的真正原因；不可為了讓 gate 變綠而改低標準或掩蓋問題。問題無法在任務範圍內
  正確修復時，保留失敗證據並回報。

### 驗收與提交

- 先跑受影響範圍的 targeted gate，再跑完整 public gate。
- 「全綠」表示相關測試、完整 pytest、Ruff lint／format、mypy、secret scan、import／
  self-test 與 `git diff --check` 全部通過。
- 私人模型、私人語料或實體硬體 gate 尚未獲准或尚未執行，不視為 public gate 失敗；必須
  明確列為「尚未驗收」，不可暗示已通過。
- 每個里程碑全綠後，可不再詢問便建立一個可回退 commit、push，並等待 GitHub Actions。
  任一 public gate 失敗時不得 commit 或 push。
- 每個里程碑一定同步 `docs/progress.md`。若功能、決策或操作方式改變，也同步更新受影響的
  README、`docs/project-decisions.md` 或 handoff 文件。
- 完成回報必須明列：錯誤、風險、尚未執行的驗收；沒有也要明確寫「無」。

## 邊界

### 永遠要做

- 保留可驗證的測試輸出；將 mock／public gate 與私人模型／硬體 gate 分開陳述。
- 變更行為時同步測試與相關文件，不讓文件把待辦寫成已完成。
- 發現超出任務範圍但會被本次變更直接破壞的行為時，修復或停止並回報，不可忽略。
- 不確定是否需要使用者授權時，先問；不要把沉默當成同意。

### 要先問我

- 讀取或修改任何私人 persona、錄音、逐字稿、設定、診斷記錄、模型或私人語料；即使只是
  唯讀也要先問。
- 啟用麥克風、耳機、實體裝置、私人模型或私人語料進行驗收。
- 安裝或升級套件、下載模型、使用 API key，或發出可能收費的請求。
- 改變架構、隱私邊界、費用策略或使用者體驗。
- 合併／替換 Hatchling 與 `setup.py` 的雙軌打包，或更換 GPT-SoVITS 的 Python 3.10／
  `sandbox-exec` 隔離；先解釋理由、風險與替代方案。
- 因官方現況而需要偏離 `PLAN.md`。

### 絕對不要做

- 不批量刪除檔案或目錄；不得使用 `del /s`、`rd /s`、`rmdir /s`、
  `Remove-Item -Recurse`、`rm -rf` 或 `git clean -fd`。需要批量清除時停止，請使用者手動
  處理。若必須刪除，只能一次處理一個已確認的明確檔案路徑。
- 不自行 force push、rebase、destructive reset，或覆寫使用者既有變更。
- 不刪除測試、不放寬 assertion、不加入不合理的 skip／ignore，也不用 mock 假裝真實 backend
  或硬體已通過。
- 不降低既有隱私與安全保護來換取功能、相容性或測試通過。
- 不偷偷偏離 `PLAN.md`，也不把尚未完成或尚未驗收的項目描述成完成。
- 不在 public gate 未全綠時 commit 或 push。
