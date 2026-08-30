# AGENTS.md

這是 Lune 的**跨 agent 共用脈絡**：這個 repo 是什麼、東西在哪、哪份檔案說了算、什麼不能弄壞、
怎麼驗收、怎麼陳述結論。任何 coding agent（Claude、Codex、Gemini）與人類維護者都適用。

Claude Code 專屬的工作方式在 [`CLAUDE.md`](CLAUDE.md)，不要把兩邊的內容互相複製。

## 專案身分

- Lune 是 macOS 14+、Apple Silicon 的 **local-first 語音陪伴桌面 App**。語音、逐字稿、人格、
  記憶、關係狀態與私人聲線資產都留在這台 Mac。
- 單一 Python 套件 `src/lune`（約 17k 行），Python 3.12、`uv`、語音管線固定於 Pipecat 1.7.0。
- 產品形態已由選單列常駐改為**視窗應用程式**（左側邊欄 + 主區），Web UI 裝在 pywebview 殼內，
  引擎是它的子行程。
- 目前是持續開發中的 MVP。測試階段預設 provider 為本機 `local_qwen`，雲端 OpenAI 延後啟用。

### 兩處刻意保留、看起來像遺留物的設計

- **Hatchling 與 `setup.py` 雙軌**：Python 套件由 Hatchling 建置，`setup.py` 只供 py2app 打包
  macOS App。`setup.py` 不是可直接刪除的舊檔。
- **隔離的第三方 runtime**：GPT-SoVITS worker 使用獨立 Python 3.10 + `sandbox-exec`，本機 Qwen
  worker 使用獨立 runtime venv。核心 engine 不與它們合併位址空間。

## 權威來源

文件會過期，程式與設定不會。查任何事實時先問「誰擁有這個值」。

| 事實 | 權威來源 |
|---|---|
| 依賴版本、Python 版本、entry points、lint／type／pytest 設定 | `pyproject.toml`（鎖定版本在 `uv.lock`） |
| macOS `.app` 打包設定（plist、`LSUIElement`、bundle 內容） | `setup.py` |
| 公開 gate 的實際內容與順序 | `.github/workflows/ci.yml` |
| provider／模型名稱與能力宣告 | `src/lune/llm/contracts.py` |
| 預設 provider 與設定 schema | `src/lune/config.py` |
| 應用狀態與 setup reason code | `src/lune/readiness.py`、`src/lune/audio/devices.py` |
| 私人資料與模型路徑 | `src/lune/paths.py` |
| IPC 協定版本、命令與事件集合 | `src/lune/ipc/contracts.py` |
| SQLite schema 與 migration | `src/lune/memory/migrations.py` |
| 管線組裝順序 | `src/lune/pipeline/factory.py` 的 `build_voice_pipeline` |
| 已 pin 的模型 revision 與逐檔 checksum | `src/lune/stt/model_manifest.py`、`src/lune/memory/embedding.py`、`src/lune/llm_spike/model_pin.py` |
| 里程碑狀態與 gate 證據 | `docs/progress.md` |
| 已定案的產品決策與理由 | `docs/project-decisions.md` |
| 使用者可見介面規格 | `docs/ui-spec.md` |
| 未完成里程碑的實作規劃 | `docs/handoff-m2-m8.md` |
| 原始方向（私人，不進 repo） | `~/Downloads/PLAN.md` |

衝突時的優先序：**程式與設定 > `docs/progress.md`（證據） > `docs/project-decisions.md`（決策）
> `docs/ui-spec.md`／`docs/handoff-m2-m8.md`（規劃） > `README.md`（對外說明）**。
文件與程式牴觸時以程式為準，並把衝突明確回報；若那是尚未做出的產品決策，不要自行裁決。

## 架構地圖

一條語音路徑，一個 generation fence：

```text
LocalAudioTransport.input → VoiceTurnGate → LuneFinalOnlySTTService
  → ContextEnricher → LLM provider → SentenceGate → TTSRouterService → PlaybackSink
```

| 套件 | 負責 |
|---|---|
| `lune.audio` | PCM 型別、Silero 聲學判斷、sample-count turn policy、pre-roll、bounded transport、預設裝置狀態機 |
| `lune.stt` | pinned MLX Whisper 的薄 adapter，只輸出 final transcript |
| `lune.llm` | provider registry、prompt、三句 gate、串流與 fallback、費用 ledger、thread 標題 |
| `lune.llm_spike` | 隔離 `mlx-lm` worker 與 spike gate 邏輯（評估用，不是 release 路徑） |
| `lune.tts`／`lune.tts_spike` | AVSpeech、沙箱化 GPT-SoVITS worker、整句 router、M0.5 安全 gate |
| `lune.memory` | SQLite migration／store、E5 檢索、rolling summary、兩階段提案、affinity、usage |
| `lune.pipeline` | 唯一組裝點、`GenerationCoordinator`、turn gate、playback fence、benchmark 評分 |
| `lune.ipc`／`lune.ui` | authenticated loopback WebSocket、`UiRuntime` 契約、pywebview 殼與內嵌 Web UI（另見 `src/lune/ui/AGENTS.md`） |
| `lune.engine`／`lune.app`／`lune.cli` | engine 子行程、桌面殼進入點、管理 CLI |

每個套件的 `__init__.py` docstring 是該層意圖的最短說明；要看模組清單請直接讀目錄，
不要在文件裡另抄一份會過期的複本。

## 不可破壞的不變式

1. **只有一條管線。** `build_voice_pipeline` 是唯一組裝點；不得為 smoke test、benchmark 或
   新 provider 另接一條簡化管線。
2. **只有一個 `GenerationCoordinator` 能推進或作廢 generation。** 所有 STT、token、tool call
   與 PCM 事件都必須帶 `generation_id`；舊 generation 永遠不得回到下游。
3. **取消順序固定，可聽輸出最先停。** `CancelEvent.audible_stop_ms` 就是 200 ms 插話門檻的
   量測值，因此 provider 排空與 transport 重建都排在它之後。
4. **插話是唯一允許音訊跨越 fence 的情況。** 打斷 Lune 的那段語音就是下一句話。裝置切換、
   STT 逾時、輸出溢位一律丟棄。
5. **只有確實送到輸出裝置的 assistant 文字才落庫。** 取消的一輪不留逐字稿、記憶或 affinity。
6. **冷啟動麥克風關閉；內建喇叭維持 `paused_unsafe_output`**（MVP 沒有回音消除）。
7. **本機模型逐檔 SHA-256 fail closed。** 永不解析 repository ID、永不隱式下載、永不接受
   浮動 revision；缺任一檔案只回報 `setup_required`。
8. **診斷只允許 allowlist 欄位。** 逐字稿、prompt、persona、記憶、API key、私人路徑與裝置 UID
   不得進入 log、`repr()`、`__str__` 或例外訊息。
9. **推理內容（`<think>`）不得進入 `SentenceGate`、TTS、記憶、SQLite 或診斷。**
10. **所有佇列都有上限。** 滿了就失敗或丟事件，不無界成長。接在 AVSpeech 後面的 bounded queue
    以「一整句」為尺度（目前 512），不得調回小值。
11. **release 的 TTS 預設是 `AVSpeechSynthesizer`**，直到私人 GPT 效能 gate 有明確通過證據。

## 信任邊界

- **Worker 隔離**：GPT-SoVITS（Python 3.10、`sandbox-exec` deny-by-default、每次啟動主動做
  file／network denial probe，probe 或 profile 失敗一律 fallback）與本機 Qwen（獨立 runtime
  venv、環境 allowlist、強制 `HF_HUB_OFFLINE`／`TRANSFORMERS_OFFLINE`、不繼承 API key 與真實
  `HOME`）。
- **Worker 不得 import 任何 `lune` module**；stdout 只走長度前綴協定，upstream 的 print 一律
  轉到由 host 丟棄的 stderr。
- **IPC** 只綁 `127.0.0.1:0`，一次性 token、單一 client、協定版本檢查與訊息上限。
- **checksum 只能證明檔案身份，不能證明 pickle／`torch.load` 安全。** 不要用「hash 對得上」
  推論「這個 checkpoint 是安全的」。
- API key 只存在 macOS Keychain，永不進 argv、設定檔、環境變數或 log。

## 可執行指令

### 設定或更新開發環境

```sh
uv sync --frozen --extra build
chflags -R nohidden .venv
uv run lune self-test
```

`chflags` 不可省略。uv 建立的 `.venv` 帶 macOS `UF_HIDDEN` 旗標，CPython 的 `site.addpackage`
會跳過帶該旗標的 `.pth`，使 editable install 失效、`import lune` 失敗，而 `pytest` 仍會全綠
（它自己設定 `pythonpath = ["src"]`）。旗標會再度出現；重跑 `chflags`，不要改
`pyproject.toml` 或重建 `.venv`。

### 完整公開 gate

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

前八項與 `.github/workflows/ci.yml` 逐一對應，`git diff --check` 是只在本機跑的第九項。
「全綠」就是上面每一項都通過，外加 GitHub Actions 成功。文件的參照檢查由 `tests/test_check_docs.py` 帶進 `pytest`，改文件時也可以單獨快跑：

```sh
uv run python scripts/check_docs.py
```

### 局部與人工執行

```sh
uv run pytest tests/audio/test_vad.py          # 先跑最接近變更的測試
uv run lune doctor                             # 本機 setup 狀態（不含私人值）
uv run lune-engine --microphone                # 需要明確的麥克風授權
uv run lune-engine --ephemeral-memory          # shakedown 對話不寫入真實資料庫
uv run python scripts/run_physical_voice_smoke.py   # 實體 gate，見 physical-gate 流程
```

### 建置 macOS App

先確認 `build/` 與 `dist/` 不存在；若存在，停止並請使用者手動處理舊產物。

```sh
uv run python setup.py py2app
```

## 驗收矩陣

依**變更的後果**選 gate，不是每次都跑全部，也不是永遠只跑單元測試。

| 變更範圍 | 必跑 |
|---|---|
| 只改文件／agent 說明 | `scripts/check_docs.py`（`pytest` 也會跑）；引用到程式時人工確認該符號仍存在 |
| 單一模組的內部邏輯 | 該模組的 targeted pytest + `ruff check` + `mypy src/lune` |
| 跨模組、管線接線、狀態機 | 上列 + 完整 `pytest` |
| 契約／IPC／SQLite schema | 上列 + 對應 contract 與 migration 測試 + `lune self-test` |
| 打包、entry point、依賴 | 上列 + `import py2app; import lune.app; import lune.engine` |
| 隱私、診斷、worker 邊界、私人路徑 | 上列 + `scripts/secret_scan.py` + 人工核對 allowlist |
| `src/lune/ui/static/*` 前端 | 上列 + `tests/ui` 契約測試（沒有 JS 測試框架，見該目錄的 AGENTS.md） |
| 延遲、音訊、實體裝置、私人模型、雲端 | 公開 gate **不構成證據**；需另行逐項授權後執行實體 gate |

## 證據與宣稱

結論一律附上證據等級，不要用「應該可以」「大概修好了」代替：

| 標籤 | 意義 |
|---|---|
| `VERIFIED` | 本機或 CI 實際跑過相關 gate，指令與輸出可重現 |
| `CODE-ONLY` | 只有程式層信心，沒有跑對應的驗證 |
| `MANUAL-VERIFICATION-REQUIRED` | 需要實體裝置、私人資產或雲端才能驗證 |
| `BLOCKED` | 缺授權、缺資產或上游阻擋，無法取得證據 |

- **deterministic fake、mock 與公開 CI 永遠不能冒充**硬體、私人模型、私人語料或雲端證據。
- 延遲、記憶體與吞吐數字**必須連同當時的電源模式與系統負載一起引用**：實測低電量模式會讓
  同一份語料的 Whisper 延遲從 1.8 s 變成 6.7 s，與程式無關。
- 未執行或未通過的 gate 一律明確寫「未執行」／「未通過」，不得省略，也不得暗示已通過。
- 不可為了讓 gate 變綠而放寬門檻、刪測試、加不合理的 skip／ignore，或用 mock 假裝真實 backend
  已通過。門檻要改是產品決策，需要使用者同意。

## 文件紀律

- 文件使用繁體中文、ATX 標題、統一表格與 fenced code block；套件名、狀態與命令用 backticks。
  程式碼與註解用英文，只有使用者可見的字串用繁體中文。`LICENSE` 保留 MIT 英文原文。
- **引用程式碼時寫「路徑 + 符號名稱」，不要寫 `路徑:行號`。** 行號會在下一次編輯就失效
  （2026-08-30 審計：`docs/ui-spec.md` 的 18 個行號錨點中有 13 個已指向錯誤位置）。
  `scripts/check_docs.py` 會擋下新的行號錨點。
- 易變的事實（模型名稱、價格、命令清單、函式清單）只寫在權威來源，其他文件指過去，
  不要各自維護一份複本。
- 每個里程碑一定同步 `docs/progress.md`。功能、決策或操作方式改變時，一併更新受影響的
  `README.md`、`docs/project-decisions.md`、`docs/ui-spec.md` 或 `docs/handoff-m2-m8.md`。
- 不要把待辦寫成已完成，也不要把「已規劃」寫成「已實作」。

## 工作方式

- 只修改目前任務需要的範圍。若本次修改會直接造成另一處 bug，可連同修正並說明因果；
  不做無關重構。
- 涉及第三方套件、OpenAI API 或 macOS 行為時，先查官方文件或原始碼，不只靠記憶。可從
  [OpenAI Developers](https://developers.openai.com/)、[Pipecat Docs](https://docs.pipecat.ai/)
  與 [Apple Developer Documentation](https://developer.apple.com/documentation/) 開始。
- 技術選擇用簡單繁體中文解釋：要改什麼、為什麼、風險與替代方案。不假設使用者熟悉 Python、
  打包工具或語音管線。
- 修正失敗的真正原因。問題無法在任務範圍內正確修復時，保留失敗證據並回報。
- 完成回報必須明列：錯誤、風險、尚未執行的驗收；沒有也要明確寫「無」。

### 提交

- 先跑受影響範圍的 targeted gate，再跑完整公開 gate。
- 每個里程碑全綠後，可不再詢問便建立一個可回退 commit、push，並等待 GitHub Actions。
  任一 public gate 失敗時不得 commit 或 push。
- 私人模型、私人語料或實體硬體 gate 尚未獲准或尚未執行，不視為 public gate 失敗；必須明確
  列為「尚未驗收」。

## 邊界

### 永遠要做

- 保留可驗證的測試輸出；mock／public gate 與私人模型／硬體 gate 分開陳述。
- 變更行為時同步測試與相關文件。
- 發現超出任務範圍、但會被本次變更直接破壞的行為時，修復或停止並回報，不可忽略。
- 不確定是否需要使用者授權時，先問；不要把沉默當成同意。

### 要先問我

- 讀取或修改任何私人 persona、錄音、逐字稿、設定、診斷記錄、模型或私人語料；即使只是唯讀。
- 啟用麥克風、耳機、實體裝置、私人模型或私人語料進行驗收。
- 安裝或升級套件、下載模型、使用 API key，或發出可能收費的請求。
- 改變架構、隱私邊界、費用策略或使用者體驗。
- 合併／替換 Hatchling 與 `setup.py` 的雙軌打包，或更換 GPT-SoVITS 的 Python 3.10／
  `sandbox-exec` 隔離；先解釋理由、風險與替代方案。
- 因官方現況而需要偏離 `PLAN.md`。

### 絕對不要做

- 不批量刪除檔案或目錄；不得使用 `rm -rf`、`git clean -fd`、`del /s`、`rd /s`、`rmdir /s`
  或 `Remove-Item -Recurse`。需要批量清除時停止，請使用者手動處理。若必須刪除，
  只能一次處理一個已確認的明確檔案路徑。
- 不自行 force push、rebase、destructive reset，或覆寫使用者既有變更。
- 不刪除測試、不放寬 assertion、不加入不合理的 skip／ignore，也不用 mock 假裝真實 backend
  或硬體已通過。
- 不降低既有隱私與安全保護來換取功能、相容性或測試通過。
- 不偷偷偏離 `PLAN.md`，也不把尚未完成或尚未驗收的項目描述成完成。
- 不在 public gate 未全綠時 commit 或 push。
