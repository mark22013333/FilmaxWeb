> 這是《FilmaxWeb 規格需求書》的第 **I** 章。索引、分期計畫與決策表在 [../規格需求書.md](../規格需求書.md)。
> 章別代號（A-4、G-6、J 的第 −1 層…）在全書通用，跨章引用時直接用代號。

## I. PDF 文件庫（第三種媒體類型）

與影片、相片並列的第三種類型，「文件」分頁。**最小可用版已實作**（本次），
封面與閱讀進度延後。

| 項目 | 狀態 |
|---|---|
| `document` 表、掃描認 `.pdf`（刪除走 `purge()`） | ✅ 已實作 |
| 「文件」分頁（檔名卡片＋資料夾 chips＋搜尋） | ✅ 已實作 |
| `/reader` 閱讀器（vendored PDF.js） | ✅ 已實作 |
| 整檔快取 + Range 服務 | ✅ 已實作 |
| 第一頁封面縮圖（pypdfium2） | ⏸ 延後 —— 要新增 Python 相依 |
| 閱讀進度 `read_state` | ⏸ 延後 |
| cmaps（無內嵌 CJK 字型的 PDF） | ⏸ 延後 —— 169 個檔案，真的遇到再補 |

### 資料層（已實作）

新表 `document`，形狀鏡像 `photo`：`ftp_path`／`folder`／`filename`／`ext`／
`size`／`mtime`／`mtime_ts`／`sort_ts`／`pages`／`probe_state`／`probe_error`／
`seen_at`／`added_at`，加 `idx_doc_folder` 與 `idx_doc_sort`。

**沒有塞進 `media_file`。** 那張表的每一列都假設自己有 `duration`、`probe_state` 的
影片語意、`play_mode`、HLS 設定檔；PDF 全部沒有。相片當初獨立開表是對的，照著做。

`SCHEMA` 用 `CREATE TABLE IF NOT EXISTS`，所以**只要重啟服務，表就自己建好，
不需要手動 migration**。

掃描：`ftpclient.DOC_EXTS = {"pdf"}`，門檻 `MIN_DOC_BYTES = 4 KB`（比這更小的
不可能是有效 PDF），走 `_index_doc()` 直接 upsert。`status.docs_found` 進掃描進度，
完成訊息加「；N 份文件」。

> **這筆技術債已經還掉（K 那一期順手做的）。**
> 原本消失的文件是在掃描迴圈裡直接 `DELETE FROM document`，而我在這裡把它
> 註解成「最小版沒有磁碟衍生物，所以現在是對的」—— **phase1 的測試不同意，
> 而測試是對的**（它釘的是「掃描器自己不寫 DELETE，一律走 purge」）。
> 現在走 `purge.purge(doc_ids=...)`。做封面時只要在 `purge()` 的第 2 步
> 多收一個 thumb 檔名即可，不必再回來改掃描器。

### 讀取路徑：整檔快取到本機（已實作）

這一節最重要的決策，理由不變：

PDF.js 預設會發大量小的 Range 請求（`rangeChunkSize` 64KB，加上 autoFetch 會
持續預抓）。而現有 `/api/stream` 的 Range 實作是「**每個請求開一條 FTP 連線、
從 offset 重讀**」—— 一份幾百頁的 PDF 會瞬間變成幾十上百條 FTP 連線，
而 FTP 併發上限實測是個位數到十幾。結果不是慢，是**把整台服務的 FTP 連線池打爆，
正在看片的人一起被拖下水**。

實作在 `routers/stream.py`：

| 常數／函式 | 值／作用 |
|---|---|
| `DOC_CACHE` | `CACHE_DIR/"docs"` |
| 快取鍵 | `d{id}_{mtime_ts}_{size}.pdf` —— 檔案換了自然換鍵，不需要 invalidate 邏輯 |
| `DOC_MAX_BYTES` | 1 GB（單檔上限，超過就拒絕而不是把磁碟吃光） |
| `DOC_CACHE_MAX_BYTES` | 2 GB，`_enforce_doc_cache()` 依 atime 做 LRU |
| `_ranged_file()` | **自己處理 206** —— Starlette `FileResponse` 的 Range 支援跨版本不一致，PDF.js 對這件事很敏感 |
| `/api/document/{id}/file.pdf` | 第一次整檔抓進快取，之後純本機讀 |

單條 FTP 實測 295 MB/s，而 PDF 通常是幾 MB —— 「先抓整檔」在體感上是一瞬間。

> **J 的第 −1 層讓這一節將來會更簡單**：片庫其實在 `D:\1.FTP`。
> 做了 `LIBRARY_LOCAL_ROOTS` 之後，PDF 可以直接 `FileResponse` 原檔，
> 整檔快取變成只在「來源真的在遠端 FTP」時才需要。

### 閱讀器：PDF.js，vendored（已實作）

`pdfjs-dist` 6.3.289（Apache-2.0）放進 `/static/vendor/pdfjs/`，理由同 PhotoSwipe。

**必須用 legacy build，這不是偏好問題。** 預設 build 呼叫
`Map.prototype.getOrInsertComputed`，那是很新的提案 —— Chromium 141 沒有、
Safari 沒有，實測直接 `TypeError: this[#Yr].getOrInsertComputed is not a function`，
整個閱讀器打不開。`版本說明.txt` 記了這件事與升級時該怎麼驗。

帶進去的檔案：`pdf.min.mjs`（519 KB）、`pdf.worker.min.mjs`（1.29 MB）、
`LICENSE`、`standard_fonts/`（16 檔）。**cmaps 刻意沒帶** —— 需要它的只有
「用了 CJK 字型但沒有內嵌」的 PDF，那 169 個檔案等真的遇到再補。

**安全設定寫死，不給選項：**

- `isEvalSupported: false`
- `enableXfa: false`、不啟用 scripting
- `standardFontDataUrl` 指向本機 vendored 目錄，不讓它抓外部資源

理由：PDF 可以內嵌 JavaScript，而這裡的 PDF 是從 FTP 來的、內容不受控。
CSP 不必放寬 —— 已經有的 `worker-src 'self' blob:`（hls.js 用的那條）
剛好也是 PDF.js worker 需要的，`script-src 'self'` 維持原樣。

### 渲染策略（已實作）

`reader.js` 三個刻意的決定：

1. **每頁先放等比例佔位框，看到才畫。** 300 頁全部 render 是幾百 MB 起跳。
   用 `IntersectionObserver`（`rootMargin: 200px`，前後各預畫 `NEAR=1` 頁），
   離開視野夠遠才 `drop()` 釋放 canvas。`MAX_DPR=2` —— 再高是拿記憶體換看不出來的銳利度。
2. **PDF 裡的 JavaScript 一律不執行**（見上）。
3. **手勢交給瀏覽器。** 沒有 `user-scalable=no`，雙指縮放是原生行為；
   自己實作一套 pinch 只會比原生的難用。

UI：連續捲動、fit 寬／fit 頁、＋／−、頁碼輸入跳頁、‹ ›、
鍵盤 ← → PageUp PageDown Space Home End ＋ −、「原檔」開瀏覽器內建檢視器。

### 實作時踩到的兩個坑（都已修，記下來避免重犯）

| 症狀 | 根因 | 修法 |
|---|---|---|
| 整份文件只有 **352 px 寬** | `.rd-doc` 是 `<main>`，`style.css` 的 `main{max-width:1800px;margin:0 auto}` 在 flex column 容器裡，cross axis 的 auto margin **會取消 stretch** → 寬度跟著內容縮，而內容寬又照容器寬算 → 卡在最小值 | `.rd-doc{width:100%;max-width:none;margin:0}` |
| 頁碼亂跳（按一次 → 2 跳到 4） | 縮小時畫面同時看得到好幾頁，`IntersectionObserver` **最後進來的 entry 會勝出** | 改用捲動位置算：rAF 節流的 `updateCurrent()`，取「頂端最接近視野上緣」那一頁 |
| **手機上切到「文件」看不到東西**（`showView()` 明明設了 `hidden`） | `[hidden]{display:none}` 只在**瀏覽器預設樣式表**裡，特異性 0,1,0 —— 和 `.toolbar`／`.grid`／`.section-title` 同級，而**作者樣式一律贏過預設樣式表**。所以 `el.hidden = true` 對任何自己寫了 `display` 的元素完全沒有作用：影片的工具列、「媒體庫」標題與格線都還留在畫面上，把文件牆往下推 **508 px**（實測 390 px 寬），手機上就是「看不到」。**相片分頁一直是同一個症狀**，只是桌機視窗高、比較不明顯 | `style.css` 加一行 `[hidden]{display:none!important}`（normalize.css 與 html5-boilerplate 都是這樣處理）。實測 docView 的 top 從 508 → 157 px，`#grid`／`.toolbar`／`#libTitle` 都真的消失。順帶修好帳號按鈕上那顆空的 `.badge` |

### 前端整合（已實作）

- `showPhotoView` 改成 `showView(kind)`，三種類型（video／photo／doc）共用一套切換。
- 文件卡片是 `<button class="doc">`，點了 `location.href = '/reader?doc=' + id`。
  **閱讀器是獨立頁面而不是 modal** —— 它要吃鍵盤、要能被加書籤、要能單獨分享。
- 搜尋框在文件分頁時路由到 `loadDocs()`。
- `/api/documents`、`/api/documents/folders`、`/api/documents/{id}`（回 `file_url`）。
- 統計加 `"documents"` 計數，掃描公開狀態加 `docs_found`。

### 驗收

已用 Playwright 對一份產生的 5 頁 PDF 驗過（headless）：

```
OPEN   {"total":"5","pageDivs":5,"canvases":2,"firstPageBox":{"w":1068,"h":1511}}
PAINT  {"size":"1068x1511","nonWhitePx":9932}          ← 真的畫出東西，不是白框
→ 鍵盤右鍵 current = 2 ／ fit 頁 = 539x763 ／ ＋ = 674x954 ／ 跳頁 5 = 5
ERRORS []
```

`canvases: 2` 是對的：5 頁只畫了看得到的那 1 頁 + 預畫 1 頁。

還沒驗、要等真實資料的：

- 加密或損壞的 PDF 要進 `probe_state='failed'` 並出現在後台失敗清單，不是靜默消失。
- 一份 300 頁 PDF 打開後，FTP 連線數不因翻頁而增加（**應該是 0 條**）。
- 唯讀使用者看得到、讀得到，但下載原檔跟相片一樣受角色控制。

### 刻意不做（第一版）

| 不做 | 原因 |
|---|---|
| 全文檢索與 OCR | 抽文字沒問題，成本在索引與 FTS 表的維護。等文件數量真的多起來再說 |
| 書籤與註記 | 註記要寫回檔案或另存側資料，兩種都是新的一整套設計 |
| EPUB／CBZ | 那是另一種排版模型（reflow）與另一種閱讀器，不要混進來 |
| 中繼資料刮削 | 沒有 TMDB 這種等級的公開書目來源可以無痛接 |
| 伺服器端把頁面轉成圖片 | PDF.js 在瀏覽器渲染就好，伺服器不必變成渲染農場 |
| 書頁翻轉動畫 | 效能與手勢複雜度的黑洞，換來的只是一次性的驚喜 |

### 封面：將來要做的話，pypdfium2，不要 PyMuPDF

| 方案 | 授權 | 判斷 |
|---|---|---|
| **pypdfium2** | Apache-2.0 / BSD-3-Clause 雙授權，PDFium 本身是 BSD 系 | **採用**。有預編譯 wheel（Windows／Linux 都有），不必裝編譯器 |
| PyMuPDF | AGPL-3.0（另有商業授權） | **不用**。自架服務只要對外提供，就落在 AGPL 的網路條款裡 |
| pdf2image | 要另外裝 poppler 執行檔 | 不用。多一個系統層依賴，Windows 上尤其麻煩 |

**取捨點是授權，不是效能。** 兩者速度都夠，而授權是不可逆的。

> **PDFium 上游明文寫「inherently not thread-safe」。** 掃描是多執行緒的，
> 所以 PDF 渲染必須收斂到單一執行緒（或一個獨立 worker 行程）——
> 探測階段只記候選，全部結束後單執行緒渲染一次，跟 G-4 的處理方式一樣。
> 這是**正確性**問題，不是效能問題。

做封面的同時要一起做的兩件事：把消失文件的刪除搬進 `purge()`，
並讓 `purge()` 清掉封面與 PDF 快取檔。

---
