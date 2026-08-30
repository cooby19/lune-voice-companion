# src/lune/ui — 子樹規則

只寫這個子樹與 repo 其他部分**不同**的地方。共用脈絡見根目錄的 `AGENTS.md`。

## 這裡的驗證條件不一樣

- `static/` 的 `index.html`／`app.css`／`app.js` 是**打包進 wheel 的 package data**：沒有建置
  步驟、沒有 npm、沒有外部依賴，也**沒有 JavaScript 測試框架**。
- 前端唯一的自動守護是 `tests/ui/` 裡以**讀取實體檔案**方式檢查的契約測試（例如核對 `app.js`
  真的處理了 runtime 會送出的每個事件名稱、真的用到 `memory_ids` 這類欄位）。
- 因此：**`tests/ui` 全綠不代表視窗行為正確。** 改 `app.js` 或 `app.css` 幾乎沒有測試網，
  結論只能標 `CODE-ONLY` 或 `MANUAL-VERIFICATION-REQUIRED`。
- 實機開窗（pywebview 視窗、殼與引擎的啟動關閉時序、視窗關閉後子行程是否回收）仍是尚未
  完成的 gate，需要另行授權才執行。

## 規格擁有權

`docs/ui-spec.md` 擁有使用者可見的介面規格與文案。實作與規格衝突時，把衝突寫進 ui-spec
並標為待裁決；**不要自行改規格**，也不要默默讓實作偏離。

## 契約

- `UiRuntime` 的**低頻 snapshot 與增量事件必須共用同一份 per-item view**
  （`_thread_view`／`_message_view`／`_memory_view`）。新增欄位要同時走兩條通道，
  否則兩邊會各自漂移。
- 事件佇列滿了要**丟事件、清掉對帳基準並強制下一次完整 snapshot**，不得阻塞引擎的寫入。
  代價是延遲，不是正確性。
- 命令與事件名稱的權威來源是 `src/lune/ipc/contracts.py`（`UI_COMMAND_NAMES`／
  `UI_EVENT_NAMES`）。名稱進了集合但沒有發送端，是缺口不是功能。

## 隱私

- 頁面以 `file:` 載入，一次性 token 由 pywebview 的 JS bridge 交付。**不得**把 token、逐字稿、
  persona、記憶內容或私人路徑寫進頁面原始碼、`console`、`localStorage` 或任何持久化位置。
- 訊息上的記憶標記只傳 id；記憶文字一律由記憶清單提供。
- 不得引入 CDN、外部字型、遠端資源或任何前端建置工具鏈。
