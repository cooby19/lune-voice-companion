# UI／UX 規格

更新日期：2026-08-30

這份文件記錄使用者可見介面的決策。在此之前，repo 只定義了引擎內部狀態名稱與 IPC 命令
名稱（`docs/handoff-m2-m8.md` 的 M7 章節），沒有任何介面規格：`src/lune/app.py` 全部 42 行，
只有一個 `Status` 選單項，點下去直接把內部字串丟給使用者。

## 兩種組成：測試階段與最終形態

2026-08-29 使用者決定，測試階段以**本機 `Qwen3.5-4B` Q4 為唯一 provider**，雲端延後規劃；
`config.toml` 的 `models.provider` 預設即為 `local_qwen`。最終形態仍是 hybrid，見下方
〈本機 LLM 備援的前提〉。

| 面向 | 測試階段（現行預設） | 最終形態（hybrid） |
|---|---|---|
| provider | 只有本機 Qwen | OpenAI 為主，本機為備援 |
| API key | 完全不需要，也不讀 Keychain | 必要，存於 Keychain |
| 網路 | 對話全程不連外 | 主力請求走雲端 |
| 費用 | 恆為零 | 依 NT$700 降級與 NT$900 鎖定兩道門檻 |
| 首句延遲 | 約 1.3 s，門檻明示豁免 | 端到端 p50 1.5 s |

**以下規格描述最終形態**，除非該處另有「測試階段」標註。凡以「雲端為主」為前提的元素
（API key 步驟、用量條、`budget_locked`、`degraded_llm`）在測試階段都不會出現。

## 產品形態變更

原計畫的 M7 是 rumps 選單列常駐程式，`setup.py` 當時設定 `LSUIElement: True`，代表沒有 Dock
圖示也沒有視窗。**本規格改為有側邊欄的視窗應用程式**，rumps 無法實作這個形態。

| 項目 | 原計畫 | 本規格 |
|---|---|---|
| 形態 | 選單列常駐，無視窗 | 視窗應用程式，左側邊欄 + 主區 |
| UI 技術 | rumps | Web UI（HTML／CSS／JS）裝在 Python 殼內 |
| 對話模型 | 單一連續 session | 多 thread，共享長期記憶與 affinity |
| 輸入方式 | 只有語音 | 語音與文字皆可 |
| LLM | 只有 OpenAI | OpenAI 為主，本機模型為備援（測試階段：只有本機） |

`LSUIElement` 必須改為 `False`；Web 殼選型已定案為 **pywebview**（見〈視窗尺寸與斷點〉）。
IPC 仍照 `docs/handoff-m2-m8.md` 的〈M7：桌面 App、IPC 與打包〉所述 authenticated WebSocket 設計，只是連線的一端從
rumps 換成 Web 殼。

### 兩條通道：增量事件為主，整包 snapshot 為對帳

一則訊息落庫不該讓每一條 thread 的私密文字重新過一次線。因此已認證連線上有兩條通道：

| 事件 | 何時送 | payload |
|---|---|---|
| `message_added` | `complete_turn()`（此時訊息才讀得到） | `{"message": {...}}`，單則，含 `thread_id` |
| `thread_updated` | 新建、改名、自動標題、turn 完成後 `updated_at` 變動 | `{"thread": {...}}`，單條 |
| `memory_updated` | `add_memory()` 成功或 `forget_memory()` 真的刪掉 | `{"memories": [...]}`，整份但有上限 |

每個 payload 用的是 snapshot 裡同一份 per-item view（`_thread_view`／`_message_view`／
`_memory_view`），所以兩條通道的欄位命名不可能各走各的。

`snapshot` 退為低頻對帳（預設 2 秒一次，且只在內容真的改變時才送），負責事件表達不了的
狀態：`app.state`、裝置、setup、persona、通話計時。送出事件同時推進對帳基準，因此一輪對話
不會再被整包重送一次。事件佇列滿了就丟事件而不是擋住引擎的寫入，被丟掉的那一刻會清掉基準，
下一次 tick 必定送出完整 snapshot——代價是延遲，不是正確性。

## 已定案的決策

- 視覺調性全程帶角色溫度：暖色深底，非中性灰階。
- 「打電話」是全畫面唯一的實心強調按鈕，位於主區右上。其餘皆為線框或純文字。
- 通話面板有展開與收合兩態，為同一元件的變形而非兩個畫面。收合態是一顆膠囊，聊天串
  完整回來，可捲動可打字。
- 通話中送出文字等同語音插話，走同一條 `GenerationCoordinator` 取消路徑。
- 文字訊息預設也會朗讀。輸入框內一顆喇叭開關可關閉；關閉時仍回覆文字。
- thread 標題在第一輪結束後自動生成一次，可手動改名。最終形態由備援模型 `gpt-5.6-luna`
  生成；測試階段由本機模型生成，兩者都不得為此另開一次雲端請求。
- 通話中切換至其他 thread 時，該 thread 為唯讀瀏覽；通話仍綁在原 thread。
- affinity 在介面上完全不提，數值與事件皆不呈現。
- 記憶刪除為逐筆硬刪，介面明確告知沒有「全部清空」。
- 沒有 API key 或 Keychain 故障時，改用本機 LLM 備援，而非停在死路。測試階段本來就沒有
  key，這條退路即是常態路徑。

## 狀態表

`AppState` 定義於 `src/lune/readiness.py`，目前有十個值；`degraded_llm` 已加入 Literal，但還沒有任何程式會設定它。

| 狀態 | 顏色語意 | 通話列文案 | 橫幅文案 | 使用者動作 |
|---|---|---|---|---|
| `setup_required` | — | — | 引導畫面接管整個視窗 | 見「第一次啟動」 |
| `mic_off` | — | 不通話，無指示 | — | 按「打給 Lune」 |
| `listening` | 強調 | 「在聽」 | — | — |
| `thinking` | 強調 | 「在想」 | — | — |
| `speaking` | 強調 | 「Lune 正在說話」＋「直接開口就可以打斷她」 | — | 開口或打字即打斷 |
| `paused_unsafe_output` | 黃：要你動手 | 「切到內建喇叭了，先停一下」 | 「接上耳機才能繼續」／「用內建喇叭的話她會聽到自己的聲音，然後就亂掉了。接上耳機會自動接回去，不用重按。」 | 「我接好了」 |
| `degraded_tts` | 灰：還能用 | 「現在是系統的聲音」 | 「她的聲線載入不了」／「先用系統合成音頂著。講的內容完全一樣，只是聽起來不像她。」 | 「看看為什麼」 |
| `degraded_llm` | 灰：還能用 | 「她現在跑在你的電腦上」 | 「連不上雲端，改用本機模型」／「反應慢一點，記性也短一點，但整段對話一個字都沒有離開這台電腦。」 | 「看看怎麼回事」「一直用本機就好」 |
| `budget_locked` | 紅：停了 | 「這個月的額度用完了」 | 「這個月已經花到 NT$900」／「為了不再扣下去，雲端先停了。下個月一號會自己解開 —— 在那之前她可以跑在你的電腦上。」 | 「改用本機」「看用量」「調高上限」 |
| `error` | 紅：停了 | 依錯誤來源 | 依錯誤來源 | 依錯誤來源 |

顏色語意共三級，使用者掃一眼即可判斷是否需要立刻處理：

- 紅：停了，雲端或整條管線不可用。
- 黃：還能修，而且需要使用者離開座位做事（目前只有接耳機）。
- 灰：她還在，只是不是完整的她。`degraded_tts` 與 `degraded_llm` 同屬此類。

呈現位置分工：**通話中一律用通話列**（即時、不打斷、不遮擋），**未通話時用主區頂部橫幅**
（資訊完整、可帶行動按鈕）。每個異常狀態因此需要兩組文案，如上表。

測試階段的兩個例外：

- `degraded_llm` 不成立。本機不是降級而是唯一路徑，因此不亮灰燈、不顯示「連不上雲端」，
  也沒有「一直用本機就好」這個動作——那已經是現況。
- `budget_locked` 不可能發生。本機 attempt 價格為零，且零價預留不受雲端鎖定阻擋
  （`src/lune/llm/budget.py`）。

### 狀態的既有實作落差

- `degraded_tts` 雖列於 `AppState`，但 `src/lune/pipeline/session.py` 的 `_idle_state()`
  只回傳 `budget_locked` 或 `DeviceStateMachine.state`（`src/lune/audio/devices.py` 的 `DeviceState` 定義的
  三個值），因此它永遠不會成為 `session.state`。目前只以
  `src/lune/pipeline/session.py` 的 `degraded_tts` sticky 屬性存在。UI 若要顯示，需另接該屬性或修改
  `_idle_state()`。
- `degraded_llm` 完全不存在，需新增。
- `src/lune/app.py` 已改為桌面殼進入點；原本的 `menu_title()` 已隨 rumps 形態一併移除。

## 版面

```
┌──────────────────────────────────────────────┐
│ ● ● ●                                        │
├──────────────┬───────────────────────────────┤
│ Lune ● 狀態  │ thread 標題   裝置  [打給 Lune]│
│ ＋ 新對話    ├───────────────────────────────┤
│              │                               │
│ 今天         │  通話面板（展開／收合／無）    │
│  thread      │                               │
│  thread      │  訊息串                       │
│ 這禮拜       │                               │
│  thread      │                               │
│──────────────┤                               │
│ Lune 記得的事│                               │
│ 你與 Lune 的 │                               │
│   設定檔     ├───────────────────────────────┤
│ 設定         │ ＋ [輸入框]        🔊  ↑      │
│ 本月 NT$412  │ 通話中送出文字會直接打斷她     │
│ ▓▓▓▓░░░░     │                               │
└──────────────┴───────────────────────────────┘
```

- 側邊欄寬度固定，可收合。

### 視窗尺寸與斷點

2026-08-30 首次實機啟動後回填。以下三項為實作現況，非重新設計。

| 項目 | 實作 | 位置 |
|---|---|---|
| Web 殼 | pywebview 6.2.1（macOS 為 WKWebView），renderer 是 bundled `file:` 頁 | `src/lune/ui/desktop.py` |
| 預設視窗 | 1280 × 820 | `desktop.py:WINDOW_WIDTH`／`WINDOW_HEIGHT` |
| 最小視窗 | 960 × 640（`NSWindow.setMinSize:`，已實測生效） | `desktop.py:WINDOW_MIN_SIZE` |
| 側邊欄收合斷點 | 840 px：側邊欄改為覆蓋式抽屜，收合鍵換成漢堡鍵 | `app.css` 的 `@media (max-width: 840px)` |
| 次要斷點 | 600 px：通話面板、橫幅、頂部列再壓縮一次 | `app.css` 的 `@media (max-width: 600px)` |

**衝突，待裁決。** 最小視窗寬度 960 px 大於 840 px 斷點，`setMinSize:` 只限制使用者拖曳
（程式呼叫 `resize()` 仍可越過，實測 840 可設定成功）。因此**使用者在原生視窗裡永遠拖不到
這兩個斷點**，除非用 ⌘+ 放大頁面。三條路擇一：

1. 把最小視窗降到 600 × 640 以下，讓兩個斷點都是真的；
2. 保留 960 最小寬，刪掉 840／600 斷點，承認這是桌面單一版型；
3. 保留斷點但改定位為「頁面縮放時的防護」，並在文件裡說明它不是視窗尺寸的斷點。

**衝突，待裁決。** 840 px 以下的側邊欄是**覆蓋式抽屜**，而非〈版面〉所寫的「寬度固定，可收合」
的同一個收合態：`.sidebar-toggle` 在該斷點下 `display: none`，收合／展開換成 `#mobile-sidebar-button`。
同一份規格因此對應兩種互動模型。

**衝突，待裁決。** 840 px 以下 `.device-button span:last-child` 被隱藏，音訊裝置只剩一顆無文字的
圓點，與「音訊裝置狀態常駐於主區頂部列」的意圖不符。
- 音訊裝置狀態常駐於主區頂部列，不放側邊欄。
- 未通話時不顯示常駐麥克風指示燈：冷啟動麥克風關閉（`docs/project-decisions.md`），
  沒在收音卻放一顆燈只會製造焦慮。若日後改為「不通話也待命」，此決定必須推翻。
- 本月用量條常駐側邊欄底部，對應 NT$700 降級與 NT$900 鎖定兩道門檻。測試階段整條隱藏：
  金額恆為 NT$0，留一條永遠空的進度條只會讓人以為壞了。

## 通話面板

| 態 | 觸發 | 內容 |
|---|---|---|
| 展開 | 按下「打給 Lune」後預設 | 頭像、聲音波形、狀態文字、通話計時、收音指示、收合鍵、掛斷鍵 |
| 收合 | 使用者下滑或按收合鍵 | 一顆膠囊：狀態點、狀態文字、計時、展開鍵、掛斷鍵。訊息串完整可見 |
| 無 | 掛斷 | 面板消失 |

展開與收合之間為同一 DOM 元件的變形，不是畫面切換。狀態點在兩態中是同一顆。

### 通話中切換 thread 的唯讀呈現

2026-08-30 實機驗證後回填。切到非通話中的 thread 時，四件事同時發生：

| 元素 | 呈現 |
|---|---|
| 通話面板 | 留在原處、維持原本的展開／收合態；通話仍綁在原 thread |
| 主區橫幅 | 「通話仍在「<原 thread 標題>」進行中；這個對話目前只能閱讀。」＋一顆「回到通話」 |
| 輸入框 | `disabled`，placeholder 換成「這個對話正在唯讀瀏覽」 |
| 頂部列 | 「打給 Lune」換成「回到通話」；「重新命名」隱藏 |

**衝突，待裁決。** 該橫幅目前套用黃色調（`.readonly-banner`），但〈狀態表〉的黃色定義是
「還能修，而且需要使用者離開座位做事」。唯讀瀏覽既非異常也不需要使用者動手，佔用黃色會稀釋
三級色彩語言。建議改為中性／灰，或另立一級「純資訊」。

**衝突，待裁決。** 輸入框已 `disabled`，但下方輔助文字仍是「通話中送出文字會直接打斷她。」，
與眼前不能打字的事實矛盾。唯讀態需要自己的一句文案。

## 第一次啟動

`src/lune/readiness.py` 的 `check_readiness()` 與 `src/lune/config.py` 的
`validate_private_setup()` 共會產生十一個 reason code，其中
兩組互斥、由 `models.provider` 決定：

| 類別 | reason code |
|---|---|
| 恆有 | `config_missing`、`config_invalid`、`persona_missing`、`persona_invalid`、`persona_unconfigured`、`whisper_model_missing`、`embedding_model_missing` |
| 只在 `openai_responses` | `keychain_unavailable`、`api_key_missing` |
| 只在 `local_qwen`（測試階段） | `local_llm_model_missing`、`local_llm_runtime_missing` |

這些不對應同樣數量的步驟。

### 行為變更

- **`config_missing` 應自我修復。** `config.toml` 內容全為有預設值的項目（匯率、預算門檻），
  第一次啟動應直接寫入一份預設檔，不得成為使用者面前的關卡。
- **`config_invalid` 不得自我修復，但也不得靜默。** 覆寫一份使用者改過、卻無法驗證的設定會弄丟
  他的資料，所以只能停下來。停下來的同時必須看得見：這兩個 config reason 都會排在五步之前，
  成為一張獨立的修復卡（`current_step` 為 `repair`），文案先卸責、附一顆「再檢查一次」。
  自我修復失敗（例如目錄不可寫）而殘留的 `config_missing` 走同一張卡。
- **`keychain_unavailable` 不是待辦事項，是系統故障。** 使用者沒有「忘了做」任何事，
  必須與其他待辦分開呈現，文案第一句先卸責。它只在雲端組成下出現。

### 五步 + 一個選配

| 步 | 對應 reason code | 內容 |
|---|---|---|
| 1 | `api_key_missing`、`keychain_unavailable` | 輸入 API key，存進 Keychain |
| 1'（測試階段取代步驟 1） | `local_llm_model_missing`、`local_llm_runtime_missing` | 檢查本機模型與 worker runtime；缺少時指出位置，不代下載 |
| 2 | `whisper_model_missing`、`embedding_model_missing` | 下載本機模型，**背景進行** |
| 3 | `persona_missing`、`persona_invalid`、`persona_unconfigured` | 填人格結構化欄位 |
| 4 | — | 麥克風權限與耳機 |
| 5（選配） | — | 私人聲線；跳過則使用系統合成音 |

步驟 2 在側邊步驟列顯示百分比，不佔用主區。使用者在模型下載時同步進行步驟 3，
不必乾等。

測試階段是**四步 + 一個選配**：沒有 API key 這件事要做，步驟 1' 只是驗證已 pin 的
`qwen-local` 與 `qwen-runtime` 存在。權重逐檔 SHA-256 fail-closed，且 repo 不提供
downloader，所以介面只能指出缺什麼、指向哪個目錄，不能代為下載。

步驟 3 使用的欄位與「Lune 這頁」設定完全相同，不做兩套介面。
`persona_unconfigured` 代表使用者仍在 example persona 上，計畫明文禁止偷偷套用，
因此此步驟不可跳過。

**步驟 4 與 5 在 setup 期間只是說明（2026-08-30，待裁決）。** setup 刻意不建立 engine，
而 macOS 的麥克風權限請求由 engine 的 CoreAudio authorizer 發出，所以原本那顆「允許麥克風
並繼續」在這個畫面上必然失敗。實作已改為說明卡加一顆「再檢查一次裝置」，權限本身照
`src/lune/ui/runtime.py` 步驟 4 的文案，在第一次按下「打給 Lune」時請求。這兩步也沒有自己的
reason code，`complete` 恆為 `false`，因此步驟列不會出現 ✓，而三步做完的下一刻 readiness
就沒有 reason、整個 setup 畫面結束。要讓步驟 4 真的能在 setup 期間完成，需要一條不依賴
engine 的權限請求路徑；那會改動裝置行為，屬於未定的產品決策。

### 文案要點

- 步驟 1 的隱私說明放在輸入框上方，不放頁尾：要人交出 API key 之前，得先讓他知道語音、
  逐字稿與記憶都不會離開這台機器。測試階段沒有這一步，對應的隱私陳述改成更強的一句：
  這個階段連文字都不會離開。
- 步驟 1 明說 key 存進 macOS 鑰匙圈，不寫進設定檔，不出現在紀錄裡。
- 步驟 5 明寫「可以之後再說」。AVSpeech fallback 為架構內建，聲線本就是選配。
- `keychain_unavailable` 文案：「這不是你少做了什麼，是系統那邊出了問題。」後接三個可執行
  的動作（重新登入帳號、確認 login 鑰匙圈未鎖、剛換過開機密碼的情況）。

## 記憶面板

資料來源為 `src/lune/memory/store.py` 的 `list_memories()`，刪除為
`src/lune/memory/store.py` 的 `forget_memory(exact_id)`。

- **依來源分組，不依時間。** 分為「你叫她記住的」與「她自己注意到的」，對應
  `StoredMemory.source`。這是使用者心裡最在意的分界：前者是委託，後者是觀察，
  後者才會讓人不安。時間退為次要資訊。
- **搜尋是語意檢索，介面必須說明。** 底層為 E5 cosine，打關鍵字找不到會被誤判為壞掉。
  搜尋框下方直接寫「她是照意思找，不是照字找」。
- **相似度不顯示數字。** 以「很接近／有點接近」呈現，cosine 分數對使用者無意義。
- **importance 以左側細線深淺呈現，不給數字。** 與 affinity 不顯示數值同一原則。
- **刪除確認須講三件事**：會從硬碟抹掉（對應 `secure_delete`）、沒有還原、**但對話紀錄
  仍在**。第三點最重要，不講會讓人以為刪記憶等於刪對話。
- **明說沒有「全部清空」。** 計畫禁止 bulk clear；與其讓人找不到，不如直接說明，
  並告知真要重來需自行刪除資料庫檔案。
- 空狀態文案：「她還沒記住什麼 —— 多聊幾次之後，她會自己把重要的事記下來。你也可以
  直接跟她說『記住這件事』。」

### 訊息上的記憶標記

一則回覆底下標出「這一輪她翻出了記憶」。這是介面裡唯一能讓使用者驗證
〈你這頁〉那句「聊到相關的事才會想起來」真的發生過的地方。

- **資料是 id，不是文字。** 來源為 `turn_retrieved_memories`（schema v3）：檢索命中哪幾筆就記哪幾筆，
  依相似度排名存放，所以介面能指出最接近的一筆。`_message_view` 只送 id，記憶文字一律由記憶清單提供，
  已刪除的記憶不可能循這條路回到訊息上。
- **命中不等於用到。** 這顆標記能保證的只有「這幾筆進了那一輪的 prompt」，不保證模型採用了它們。
  文案因此是「她想起了一件事」，不是原稿的「來自她記得的事」——後者宣稱的是因果。
  文案不得升級成因果宣稱。
- **刪除不留殘影。** 硬刪記憶時外鍵 cascade 一併刪掉關聯，標記隨之消失；`forget_memory` 回傳的
  snapshot 現在會被 `app.js` 套用，所以是當下消失，不是等下一次對帳。
- **不回填。** 這條鏈只從接上的那一刻起生效，既有對話永遠不會有標記。

## 你與 Lune 的設定檔

單一入口，兩頁。

### 你這頁

| 欄位 | 說明 |
|---|---|
| 她怎麼叫你 | 單行文字 |
| 你想讓她知道的事 | 多行自由文字，每次對話都會帶上 |

頁底必須有一塊說明 profile 與記憶的差別，這是使用者唯一需要建立的心智模型：

> 這頁是**你親口說的** —— 永遠會進對話，她一定看得到。
> 那邊是**她自己注意到的** —— 聊到相關的事才會想起來。

並直接連往記憶面板。

### Lune 這頁

底層寫回 `persona/kernel.yaml`，但只開放結構化欄位，不開放編輯整份 YAML。

| 欄位 | 型態 |
|---|---|
| 說話的語言 | 中英比例滑桿 |
| 主動程度 | 三選一 |
| 回話長度 | 二選一 |
| 她的聲音 | 私人聲線／系統合成音 |

**不開放修改的部分**必須明列於同一頁，不得隱藏：

- 她不會假裝自己是人
- 她不會刻意讓你離不開她
- 不知道的事她會說不知道

並附上一句「不是技術限制，是刻意的。這幾件事一鬆開，剩下的設定就沒有意義了。」
藏起來會顯得心虛。

儲存後需提示：改完之後她會馬上不太一樣，但既有 `summaries` 是照舊個性寫的，不會回溯重寫。

## 對 engine 的缺口

| 缺口 | 目前狀況 | 位置 |
|---|---|---|
| `degraded_llm` 狀態 | 名稱已在 `AppState` 內，但沒有任何程式會設定它（測試階段也不需要） | `src/lune/readiness.py` |
| `degraded_tts` 不可達 | sticky 屬性不進 `_idle_state()` | `src/lune/pipeline/session.py` |
| 文字輸入入口 | **已接**：`submit_text()` 與語音共用同一條 fenced turn path，文字送出即插話 | `src/lune/pipeline/session.py` |
| 文字訊息落庫條件 | **已接**：關閉朗讀時由 `append_assistant_text_delivery()` 逐句落庫 | `src/lune/memory/store.py` |
| `config_missing` 自我修復 | **已接**：第一次啟動寫入預設檔；`config_invalid` 另走修復卡 | `src/lune/readiness.py`、`src/lune/ui/runtime.py` |
| 本機 LLM provider | **已接**：registry、pipeline 與 release 預設皆已切換；實體 gate 未跑 | `src/lune/llm/local_qwen.py` |
| 本機組成的第一次啟動 | 步驟 1' 與兩個新 reason code 尚無介面 | `src/lune/readiness.py` |
| authenticated WebSocket IPC | **已接**：一次性 token 與單一 client；`message_added`／`thread_updated`／`memory_updated` 已有發送端，snapshot 退為對帳 | `src/lune/ipc/server.py`、`src/lune/engine.py` 的 `run_ui_ipc()` |
| `get_status` | **已接** | `src/lune/ui/runtime.py` |
| 訊息上的記憶標記 | **已接**：檢索 id 由 enricher 保留、隨 turn 提交寫入、`memory_ids` 兩條通道共用；點擊行為仍未定 | `src/lune/pipeline/enricher.py`、`src/lune/memory/store.py`、`src/lune/ui/runtime.py` |
| `budget_changed` | 名稱在 `EVENT_NAMES` 內，但沒有發送端，`app.js` 也沒有對應分支 | `src/lune/ipc/contracts.py` |
| Web 殼 | **已定案**：pywebview 6.2.1，首次實機啟動已通過（一次性 token 經 JS bridge 交付、WebSocket 接上、本機 Qwen 完成一輪對話） | `src/lune/ui/desktop.py` |
| `LSUIElement` | **已改**：plist 已是 `False`，App 有 Dock 圖示與視窗 | `setup.py` |
| 應用程式圖示 | `iconfile` 為 `None`，repo 無任何圖形資產 | `setup.py` |
| UI 語言 | **已接**：內嵌 Web UI 與 `NSMicrophoneUsageDescription` 皆為繁中 | `src/lune/ui/static/`、`setup.py` |

### 本機 LLM 備援的前提

`docs/progress.md` 的〈後續交接〉記錄的 spike 結論為：`Qwen3.5-4B` Q4 在目標硬體上行為
全數正確、資源充裕，僅首句延遲 1,337 ms 未達 1,150 ms 門檻。該門檻由「主力模型」的端到端
p50 1.5 s 反推而得。**作為備援時此門檻不適用** —— 離線或預算鎖定時，1.3 秒的首句遠優於
無法對話。該處等待的產品決策（hybrid／維持 OpenAI／更小模型）於本規格確定為
hybrid：OpenAI 為主，本機為備援。

**測試階段把主備倒置**，理由與差異見本文件開頭的〈兩種組成〉，決策全文見
`project-decisions.md` 的〈測試階段的 LLM 組成〉。倒置只改哪一邊是主力，不改上面這段
論證：門檻本來就是為「主力」訂的，而本機在兩種組成裡都不是主力所服務的那個角色。

## 尚未決定

2026-08-30 首次實機啟動後，Web 殼選型、視窗最小尺寸與收合斷點、通話中唯讀呈現三項已定案並
回填至上文；它們留下的三個衝突改列於〈視窗尺寸與斷點〉與〈通話中切換 thread 的唯讀呈現〉，
標為「待裁決」。以下為仍未決定的項目。

- `budget_locked` 的顏色歸類。本機備援使其從死路變為分岔，是否仍屬「停了」需再議。
  測試階段不會遇到，可留到雲端接回時再定。
- 測試階段的通話列是否要有任何「跑在本機」的常駐提示。**實作目前照「不要」執行**：通話面板
  只有狀態、計時與波形；「只在這台電腦上」的陳述放在側邊欄底部與設定頁。此決定尚未正式定案。
- 訊息上「她想起了一件事」標記**點擊後**的行為。資料鏈已接，標記會真的出現，但點下去目前只切到
  記憶面板、不高亮任何一筆（`app.js` 的 `show-memories` 分支收下 `memoryId` 卻沒有用）。
  要做高亮就得連同記憶面板的 16 筆上限一起決定：超出上限的記憶就算被標記指到，也不在那份清單裡。
- setup 期間是否要提供一條不依賴 engine 的麥克風權限請求，讓步驟 4 能真的完成並顯示 ✓。
  **實作目前照「不要」執行**，理由與後果見〈五步 + 一個選配〉。
- 應用程式圖示與選單列圖示的視覺設計。
- 異常狀態的兩組文案是否要真的分開。〈狀態表〉為每個狀態各寫了「通話列文案」與「橫幅文案」，
  但 `app.js` 的 `STATUS_COPY` 每個狀態只有一組 `label`／`subtitle`，兩處共用。實機上
  `paused_unsafe_output` 的橫幅標題因此是通話列的「切到內建喇叭了，先停一下」，而不是規格寫的
  「接上耳機才能繼續」。要嘛擴成兩組，要嘛把〈狀態表〉收斂成一組。
