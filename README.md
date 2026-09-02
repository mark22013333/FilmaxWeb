# FilmaxWeb — FTP 影音庫 × 網頁串流

![status](https://img.shields.io/badge/status-持續更新中-brightgreen)
![python](https://img.shields.io/badge/python-3.10%2B-blue)
![ffmpeg](https://img.shields.io/badge/ffmpeg-required-orange)
![license](https://img.shields.io/badge/用途-僅限個人合法內容-red)

把 Filmax（小雲盒子那類 Android TV 影音 App）的核心流程搬到自架網頁服務：
**掛載 FTP → 掃描檔案 → 解析檔名 → TMDB 刮削 → 海報牆 → 瀏覽器串流播放**。

啟動後打開 `http://localhost:8080`，同一個區網的手機、平板、電視瀏覽器都能連進來看。

> ⚠️ **使用前請先讀[使用聲明與授權範圍](#使用聲明與授權範圍)。**
> 本專案僅供管理**你自己合法持有**的影音檔案，**嚴禁用於任何非法用途**。

> 🚧 **本專案持續開發中。** 功能與設定項目會隨版本調整，
> 更新後請一併看過 `.env.example`（可能有新增設定）與本文件的
> [開發進度](#開發進度)。

```bash
git clone https://github.com/mark22013333/FilmaxWeb.git
cd FilmaxWeb
cp .env.example .env      # 填入你的 FTP 連線資訊
```

---

## 1. 它做了什麼（對照 Filmax）

| Filmax 的行為 | FilmaxWeb 對應做法 |
|---|---|
| 加入 FTP/SMB 來源 | `.env` 設定 FTP 連線與影片根目錄，可設多個並標記 `movie` / `tv` |
| 掃描出影片檔 | 遞迴走訪 FTP（MLSD 優先，退回 LIST / NLST 解析），只收錄影片副檔名且大於門檻的檔案 |
| 從檔名辨識片名 | `nameparser.py`：剝掉 `1080p/x265/WEB-DL/中英雙語/發布組` 等雜訊，抓出片名、年份、`S01E03`／`第二季 第10集`／動畫的 `- 12`，中英片名並列時兩個都拿去搜 |
| 抓封面、簡介、分類 | TMDB API：搜尋 → 取詳情 → 下載海報/劇照到本地 `data/images`，影集連每一集的標題與劇照都抓 |
| 海報牆 + 分類瀏覽 | 網頁海報牆，可依類型（電影/影集）、TMDB 類型標籤、片名搜尋、排序（加入時間/片名/年份/評分）過濾 |
| 點下去就播 | 依格式自動選：**直接串流**（HTTP Range）或 **即時轉碼**（隨選分段 HLS） |
| 記住看到哪 | 每 5 秒回寫播放進度，首頁有「繼續觀看」 |

額外多的：外掛/內嵌字幕轉 WebVTT、手動指定 TMDB 修正配對、原始檔下載、轉碼快取管理。

---

## 2. 安裝

需求：**Python 3.10+** 與 **ffmpeg**（轉碼播放與縮圖用；只播 mp4 的話可省略）。

Windows 安裝 ffmpeg 最快的方式：
```
winget install Gyan.FFmpeg
```
裝完重開一個終端機，`ffmpeg -version` 有反應就好了。

### 啟動

1. 把 `.env.example` 複製成 `.env`，填入 FTP 連線資訊與 TMDB 金鑰
2. 雙擊 **`啟動.bat`**（Linux/macOS 用 `./start.sh`）

首次執行會自動建立 `.venv` 並安裝套件。之後再啟動只要 3 秒。

TMDB 免費金鑰申請：<https://www.themoviedb.org/settings/api>（選 Developer，v3 auth 那組 API Key）。
沒填也能用，只是沒有海報與簡介，海報位置會改用影片截圖。

---

## 3. `.env` 重點設定

```ini
FTP_HOST=192.168.1.10
FTP_PORT=21
FTP_USER=admin
FTP_PASSWORD=xxxx
FTP_ENCODING=utf-8          # 中文檔名亂碼時改成 gbk 或 big5

# 多個根目錄用 ; 分隔，路徑|類型
LIBRARY_ROOTS=/Movies|movie;/TVShows|tv;/Downloads|auto

TMDB_API_KEY=你的金鑰
TMDB_LANGUAGE=zh-TW

MIN_FILE_MB=50              # 小於這個大小的檔案不收錄（濾掉預告片/樣本檔）
SCAN_EXCLUDE_DIR_PREFIXES=_,temp  # 略過名稱以這些開頭的資料夾（整棵子樹）
SCAN_EXCLUDE_EXTS=iso,ts    # 掃描時略過這些副檔名（大小寫不分，加不加點都行）
SCAN_ONLY_EXTS=             # 只收這些副檔名，留空=不限制
MIN_PHOTO_KB=40             # 小於這個大小的圖片不收（濾掉圖示）
MAX_PHOTO_MB=80             # 超過這個大小的圖片不讀
FFMPEG_HWACCEL=nvenc        # NVIDIA 獨顯。也可填 auto 讓程式自己實測挑一個
TRANSCODE_MAX_HEIGHT=1080   # 4K 片源轉成 1080p 播，省 CPU 也省頻寬
PORT=8080

AUTH_ENABLED=true
AUTH_PASSWORD=管理員密碼      # 完整權限
VIEWER_PASSWORD=唯讀密碼      # 只能看與播，要分享給別人就給這組

# Google 帳號登入（可選）。設了之後登入頁會多一顆「使用 Google 帳號登入」，
# 密碼登入仍然保留；只填這一段不設密碼，就是純帳號制。
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
PUBLIC_BASE_URL=https://video.example.com
GOOGLE_ADMIN_EMAILS=你的Gmail  # 第一個管理員，沒設的話所有人都會卡在待審核

# 使用者資料存 MSSQL（留空 = 存本機 SQLite）
MSSQL_HOST=
MSSQL_DATABASE=filmax
MSSQL_USER=filmax_app
MSSQL_PASSWORD=

# 明講要用哪一份。留空 = 自動判斷（MSSQL 設好就走 MSSQL）
USER_STORE=          # sqlite / mssql
MEDIA_STORE=         # 媒體庫預設一律 sqlite，要搬過去必須自己填 mssql
```

完整清單看 `.env.example`，每一項都有註解。**版本更新後記得回頭看一次**，
可能有新增的設定項目。

> 資料庫、海報快取與轉碼分段預設放在專案底下的 `data/`。轉碼快取會長到好幾 GB，
> 想搬到別的磁碟就設**環境變數** `FILMAX_DATA_DIR`（要真的設在環境變數，
> 不能寫在 `.env` —— 路徑在載入 `.env` 之前就要決定好）。

`SCAN_EXCLUDE_DIR_PREFIXES` 比對的是**資料夾名稱的開頭**（不是完整路徑），
所以同名資料夾放在哪一層都會被跳過，而且是**整棵子樹都不走訪** ——
省下的是列目錄的時間，不只是過濾檔案。設 `_,temp` 的話 `_old`、`TempWork`
都會被跳過；注意前綴比對的必然結果是 `Temperature` 這種也會中。

除此之外，`@eaDir`、`#recycle`、`sample`、`BDMV` 等常見的雜訊目錄
與所有 `.` 開頭的隱藏目錄本來就會跳過，不必自己設定。

`LIBRARY_ROOTS` 的類型欄位很有用：標成 `tv` 的目錄即使檔名沒有 `S01E01`，也會被當影集處理；
標成 `auto` 就交給檔名解析器自己判斷。

---

## 4. 播放模式：為什麼分兩種

瀏覽器的 `<video>` 只認得少數幾種組合（MP4/WebM 容器 + H.264/VP9/AV1 + AAC/Opus）。
FTP 上的片子多半是 **MKV + HEVC + AC3/DTS**，丟給瀏覽器只會黑畫面或沒聲音。

所以掃描時會用 `ffprobe` 探測每個檔案，然後：

* **直接串流（direct）** — mp4/webm 且編碼瀏覽器吃得下 → 原始位元組直送，支援 Range 拖曳，伺服器 0 CPU。
* **即時轉碼（HLS）** — 其餘全部 → 切成 6 秒一段，**哪一段被要才轉哪一段**，轉完存到 `data/hls` 快取。

隨選分段的好處是進度條可以直接跳到任何位置（不用等前面轉完），而且看第二次是吃快取、瞬間開。
播放時還會背景預轉後面 3 段，讓畫面不會卡。

播放頁右上角會顯示目前用哪一種，也可以手動切換試試看。

### HDR 片源

HDR / Dolby Vision 的片子是 BT.2020 + PQ 曲線，直接當成一般 SDR 送給瀏覽器會**發灰、顏色不對**。
掃描時會從 `color_transfer` 判斷（`smpte2084` = HDR10、`arib-std-b67` = HLG），
播放時自動掛上色彩轉換濾鏡。播放器標題列會顯示目前用哪一種。

| `HDR_TONEMAP` | 做法 | 相對成本 | 結果 |
|---|---|---|---|
| `quality` | 轉進線性光 → tonemap → 轉回 BT.709 | **約 1.8 倍** | 高光滾降自然，畫質最好 |
| `fast` | 直接做轉換特性/色域轉換 | 約 1.2 倍 | 色彩正確，高光會被削掉 |
| `auto`（預設） | 4K 用 `fast`、其餘用 `quality` | — | 依成本自動選 |
| `off` | 不處理 | 1 倍 | 畫面發灰、顏色偏掉 |

`auto` 這樣分是有原因的：實測完整 tonemap 慢 1.8 倍，4K 片源在一般 CPU 上會掉到
即時速度以下而卡頓，`fast` 只慢兩成卻能把色彩救回來。

需要 ffmpeg 有編進 **libzimg**（`zscale` 濾鏡）。Gyan 的 full build 有；缺的話
播放器會標示「HDR 未處理」而不是默默給你錯的顏色。

### H.264 level

輸出解析度決定 level，不再寫死。這不是小事 —— 原本硬寫 `4.1` 遇到 4K 輸出時，
x264 會警告 `MB rate > level limit` 但**照樣把 4.1 蓋進串流**，產出不合規的檔案，
嚴格的解碼器（電視、硬體播放器）會拒播。

| 輸出高度 | level |
|---|---|
| ≤1080p | 4.1 |
| ≤1440p | 5.0 |
| ≤2160p | 5.1 |
| 更高 | 5.2 |

### 硬體加速：編碼與解碼是分開的

`FFMPEG_HWACCEL` 管編碼（NVENC/QSV/AMF），`FFMPEG_DECODE_HWACCEL` 管解碼（NVDEC 等）。
兩者是不同的硬體單元、不同的驅動要求 —— **舊顯卡常見「編碼被驅動擋掉、解碼還能用」**，
這時解碼加速仍然值得開，尤其 4K HEVC 的解碼成本很高。

編碼器的參數會**實測**過才用：先用最保守的參數確認硬體在不在，再從畫質最好的參數
往下試，第一個能用的就固定下來。所以換了 ffmpeg 或驅動導致某個參數不被接受時，
程式會自動退到能用的那一級，而不是整個掉回 CPU。網頁右上角 ⓘ 看得到實測過程。

### 效能參考

一段 6 秒的 1080p 用 `libx264 veryfast` 大約 1～2 秒轉完（一般桌機 CPU），也就是轉得比播得快。
如果卡頓，依序試：

1. `.env` 設 `FFMPEG_HWACCEL=auto`。**但先確認硬體加速真的划算** ——
   舊顯卡（例如 GTX 10 系列）的編碼器畫質明顯輸給 x264，而且如果 CPU 夠快，
   NVENC 未必比較快。ⓘ 面板會顯示實測結果與失敗原因。
2. CPU 編碼調 `X264_PRESET`：往 `faster` / `fast` 調畫質更好但更慢。
   先看播放時主控台印的「x 即時」還有多少餘裕

   > 啟動時程式會**真的丟一小段影片去試編一次**，硬體編碼器沒裝好或驅動不支援會自動退回 CPU，
   > 不會整個播不動。實際選到哪一個，在網頁右上角 ⓘ 的「轉碼器」那行看得到。
3. 調高 `TRANSCODE_CRF`（23～26 畫質略降但快很多）
4. 降 `TRANSCODE_MAX_HEIGHT=720`
5. HDR 片改 `HDR_TONEMAP=fast`

---

## 5. 使用流程

1. 開 `http://localhost:8080`
2. 右上角 **掃描媒體庫** → 右下角面板會即時顯示進度（列目錄 → 刮削 → 分析格式）
3. 掃完就是海報牆。點卡片看詳情、點「播放」開播放頁
4. 刮錯的片子：詳情頁 → **手動指定 TMDB** → 輸入正確片名 → 選對的那一部

之後新增檔案只要再按一次掃描，已存在且大小沒變的檔案會直接跳過，很快。

> 影集被拆成一張一張卡片的話（刮不到 TMDB 的片子最常見），按右上角的
> **重新分組**。它不重掃 FTP，只把已經在資料庫裡的檔名重新解析一次 ——
> 幾秒就跑完，不必等一次完整掃描。分組規則見下一節。

### 檔名怎麼被分成「一部影集」

`S01E02` 這種標準寫法一向沒問題，難的是沒有標準寫法的那些。多加了兩條規則：

**一集一個資料夾。** `火影忍者/01/`、`火影忍者/02/`⋯⋯ 這種結構，
資料夾名字本身就是集數。要**同層至少有兩個純數字資料夾**才成立，
免得把單一個叫 `2` 的資料夾當成影集。

**檔名結尾的數字。** `名偵探柯南 1.mkv`、`名偵探柯南 2.mkv`⋯⋯
要**同層至少兩個檔案前綴相同、數字不同**才算。

兩條規則都還要再過一關：那串數字**看起來像不像集數**。
判準是「有沒有補零」（`01`、`02` → 是集數）或「有沒有從頭開始編號」
（最小值 ≤ 2 → 是集數）。這一關是為了擋掉續集電影 ——
`玩命關頭 7/8/9` 同層、前綴相同、數字不同，完全符合前兩條規則，
但它們是三部電影不是三集，而 7、8、9 既沒補零也不從 1 開始。

---

## 6. 專案結構

```
filmax-web/
├─ 啟動.bat / start.sh      一鍵啟動
├─ run.py                   進入點
├─ .env.example             設定範本
└─ app/
   ├─ config.py             設定載入
   ├─ db.py                 SQLite schema 與存取
   ├─ ftpclient.py          FTP 連線池、目錄走訪、Range 位元組串流
   ├─ nameparser.py         檔名 → 片名/年份/季集
   ├─ scraper.py            TMDB 搜尋、詳情、圖片下載
   ├─ media.py              ffprobe 探測、播放模式判定、縮圖、字幕
   ├─ hls.py                隨選分段轉碼與快取
   ├─ scanner.py            掃描流程編排
   ├─ auth.py               登入驗證、角色、session token、來源 IP 判斷
   ├─ oauth.py              Google OAuth：state/nonce/PKCE、id_token 驗證
   ├─ users.py              使用者帳號的門面：政策、快取、挑後端
   ├─ users_sqlite.py       帳號存本機 SQLite
   ├─ users_mssql.py        帳號存 dbo.filmax_users
   ├─ mssql.py              MSSQL 連線、重連、時間換算、健康檢查
   ├─ audit.py              稽核紀錄（本機一定寫，MSSQL 有設就一起寫）
   ├─ dbcheck.py            MSSQL 逐項檢查與資料搬移（python -m app.dbcheck）
   ├─ geo.py                從 Cloudflare 標頭取來源位置
   ├─ photo.py              圖片尺寸與 EXIF（刻意不讀 GPS）、縮圖
   ├─ timeparse.py          FTP／EXIF 的五種時間格式 → epoch 整數
   ├─ purge.py              唯一的刪除進入點與孤兒清掃
   ├─ params.py             系統參數登記表：分層、生效方式、秘密加密、日誌過濾
   ├─ paramstore.py         設定解析（env > DB > 預設）與 CLI 救援路徑
   ├─ ftpprobe.py           量 FTP 連線上限與速度，推薦併發數（python -m app.ftpprobe）
   ├─ routers/api.py        媒體庫 API、帳號審核 API
   ├─ routers/auth_google.py  /auth/google/start 與 /callback
   ├─ routers/stream.py     串流 / HLS / 字幕端點
   └─ static/               海報牆、播放器前端（含 hls.js）

cloudflare/                 Cloudflare Tunnel 設定腳本與說明

db/                         MSSQL 使用者資料表
├─ 00_create_database.sql   建立資料庫與兩組帳號（只跑一次）
├─ flyway.conf              Flyway 設定（真實位址在 gitignore 的 flyway.local.conf）
├─ migrate.bat              套用 Flyway 遷移
├─ 檢查連線.bat              逐項檢查連線、權限、資料表
└─ sql/V1~V5.sql            遷移檔

docs/規格需求書.md          後續開發的規格（管理後台、系統參數、媒體庫上 MSSQL）

e2e_test.py                 端對端測試（只用標準函式庫）
tests/oauth_test.py         Google 登入的端對端與攻擊面測試（可切兩種儲存後端）
tests/mssql_test.py         MSSQL 後端的時區、欄位長度、斷線行為
tests/fake_pyodbc.py        測試用的 pyodbc 頂替品（後面接 SQLite）
tests/phase1_test.py        分組規則、時間解析、相片過濾
tests/scan_e2e_test.py      對真的 FTP 伺服器跑完整掃描
tests/sql_params_test.py    SQL 佔位符與參數數量（AST 靜態檢查）
tests/phase2_test.py        批次寫入、purge 連帶刪除、sweep 寬限期、時間值域
tests/phase3_test.py        設定解析順序、分層、秘密、漂移、後台權限
tests/env_drift_test.py     程式讀的設定 ↔ .env.example
tests/browser_test.py       真的 Chromium：換頁捲動、全螢幕、手機版面
安裝.bat                     一鍵安裝：Python、ffmpeg、venv、套件
```

### 一個關鍵設計

**ffmpeg 不直接連 FTP**，而是去讀本服務自己的 `http://127.0.0.1:PORT/api/stream/{id}`。
所有 FTP 通訊（含 GBK/Big5 檔名編碼、FTPS）都收斂在 Python 這一層，
ffmpeg 只看到一個支援 Range 的乾淨 HTTP 來源，可以任意 seek。
這樣就完全避開 ffmpeg 內建 ftp protocol 對非 UTF-8 檔名的相容性坑。

---

## 7. API

服務啟動後 `http://localhost:8080/api/docs` 有完整的互動式文件。常用的：

🔒 = 需要管理員角色，唯讀帳號會收到 403。

| 端點 | 說明 | |
|---|---|---|
| `GET /api/me` | 目前登入者：角色、email、頭像、待審核人數 | |
| `GET /api/library?kind=&genre=&q=&sort=&page=` | 媒體庫列表 | |
| `GET /api/items/{id}` | 詳情（影集含季/集/檔案） | |
| `GET /api/play/{file_id}?h=&a=` | 播放資訊：模式、時長、音軌、字幕軌、續播位置。`h` 指定畫質、`a` 指定音軌 | |
| `GET /api/stream/{file_id}` | 原始檔串流（支援 Range） | |
| `GET /api/hls/{file_id}/index.m3u8?p=` | HLS 播放清單 | |
| `GET /api/subtitle/{file_id}/embedded/{index}.vtt` | 內嵌字幕轉 WebVTT（邊抽邊送） | |
| `GET /api/subtitle/{file_id}/external.vtt?path=` | 影片旁的外掛字幕轉 WebVTT | |
| `GET /api/prefs` · `POST /api/prefs` | 播放器偏好（Google 帳號每人一份，密碼登入依角色分） | |
| `POST /api/progress` · `GET /api/continue` | 播放進度與繼續觀看 | |
| `GET /api/download/{file_id}` | 下載原始檔 | 🔒 |
| `POST /api/scan` | 開始掃描（`?full=true` 全部重刮） | 🔒 |
| `GET /api/scan/status` | 掃描進度 | |
| `POST /api/probe/{file_id}` | 重新分析單一檔案 | 🔒 |
| `POST /api/rescrape/{item_id}?tmdb_id=` | 手動指定 TMDB 配對 | 🔒 |
| `GET /api/ftp/browse?path=` | 瀏覽 FTP 目錄（限 `LIBRARY_ROOTS` 之內） | 🔒 |
| `GET /api/diagnostics` | ffmpeg / FTP / TMDB / 使用者資料庫 一次檢查 | 🔒 |
| `POST /api/cache/clear` | 清除轉碼與字幕快取 | 🔒 |
| `GET /api/photos?folder=&q=&sort=&page=` | 相片列表 | |
| `GET /api/photos/folders` | 有相片的資料夾與張數 | |
| `GET /api/photos/stats` | 相片庫統計 | |
| `GET /api/photos/{id}` | 單張詳情（尺寸、EXIF） | |
| `GET /api/photo/{id}/thumb.jpg` | 縮圖 | |
| `GET /api/photo/{id}/full` | 原圖 | |
| `GET /api/audit/logins?limit=` | 最近的登入紀錄（IP、位置、UA、email） | 🔒 |
| `POST /api/session/revoke-all` | 讓所有裝置的登入立刻失效 | 🔒 |
| `GET /api/users?status=` | 使用者清單與各狀態人數 | 🔒 |
| `PATCH /api/users/{id}` | 核准／拒絕／停權、改角色、加備註 | 🔒 |
| `DELETE /api/users/{id}` | 刪除帳號 | 🔒 |

Google 登入另外有兩個不在 `/api` 底下的端點（不需登入即可存取）：
`GET /auth/google/start?next=` 開始授權、`GET /auth/google/callback` 接 Google 的回呼。

---

## 8. 對外開放：登入驗證與遠端畫質

### 登入

`.env` 設定：

```ini
AUTH_ENABLED=true
AUTH_PASSWORD=你的密碼
SESSION_DAYS=30
```

啟用後所有頁面與 API 都需要登入，session 用 HMAC 簽章的 cookie（沒有額外相依套件），
簽章金鑰第一次啟動時自動產生在 `data/secret.key`。連續 8 次密碼錯誤會鎖該 IP 15 分鐘。

放在 nginx / Cloudflare 之類的反向代理後面時要加 `TRUST_PROXY=true`，
才會用 `X-Forwarded-For` 判斷來源 IP（不然所有人看起來都來自代理伺服器）。

> **本機的 ffmpeg 走的是另一條路。** 轉碼時 ffmpeg 要讀 `/api/stream/{id}`，
> 但它沒有瀏覽器的 cookie。程式在每次啟動時產生一組隨機 token，
> **只有從 127.0.0.1 且帶著這組 token 的請求**才會被放行，其他一律要登入。

### Google 帳號登入（審核制）

密碼登入的問題是「一組密碼給很多人」：誰在看、什麼時候看、要收回某一個人的權限，
全都做不到。設定 Google 登入之後，每個人有自己的帳號、自己的進度與偏好，
權限可以單獨收回。

```ini
GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=xxx
PUBLIC_BASE_URL=https://video.example.com
GOOGLE_ADMIN_EMAILS=你的Gmail          # 第一個管理員
GOOGLE_ALLOWED_DOMAINS=               # 限定網域，留空 = 任何 Google 帳號都能申請
GOOGLE_AUTO_APPROVE=false             # 新帳號自動核准，對外開放時建議維持 false
```

**申請憑證**（Google Cloud Console）：

1. 建立專案 → **API 和服務 → OAuth 同意畫面**，User type 選「外部」，
   然後按 **發布應用程式**。留在「測試中」的話只有你加進測試名單的人能登入，
   而且 refresh token 七天就過期。
2. **憑證 → 建立憑證 → OAuth 用戶端 ID → 網頁應用程式**
3. 「已授權的重新導向 URI」填 `PUBLIC_BASE_URL` + `/auth/google/callback`，
   例如 `https://video.example.com/auth/google/callback`。
   Google 只接受 **https**（`http://localhost` 例外），純 IP 位址一律不收，
   而且必須一字不差 —— 結尾多一個斜線都會被拒。
4. 把用戶端 ID 與密鑰貼進 `.env`，重新啟動。

**設了 Google 登入就會自動打開 `AUTH_ENABLED`。** 不然會出現一個很糟的組合：
登入頁上有 Google 按鈕，但中介層在 `AUTH_ENABLED=false` 時根本不驗證，
等於前門上鎖、後門大開。

**流程**：任何人都可以用 Google 帳號送出申請，但**預設不會放行** ——
第一次登入只會看到「等待管理員核准」，看不到任何影片。管理員在右上角
**帳號** 面板核准之後，對方用同一個帳號再登入一次就能進來，不必重新申請。

`GOOGLE_ADMIN_EMAILS` 是**第一個管理員的唯一入口**。沒設的話所有人都會卡在待審核，
而且沒有任何人有權限去核准 —— 服務啟動時偵測到這個狀況會印警告。
名單裡的信箱一登入就是管理員且免審核；已經存在的帳號也會在下次登入時被提升
（讓你可以「先登入、事後才想到要設名單」而不必去改資料庫）。

**帳號狀態**：`pending`（待審核）→ `approved`（可用）／`rejected`／`disabled`。
只有 `approved` 進得來，而且**每一個請求都會再確認一次**，不是只在發 cookie 的時候看。
停權或降級會讓對方手上的 session **立刻失效**（角色與版本號都納入 token 簽章），
不必等 30 天過期。

> 最後一個管理員不能停權、不能降級、不能刪除。這不是體貼，是避免一個無法從
> 網頁救回來的死局 —— 一旦沒有任何管理員，就只剩下去改資料庫一條路。

**安全設計**：授權用 authorization code + PKCE(S256)；`state` 與 `nonce` 放在
HMAC 簽章的短效 cookie 裡（10 分鐘），回呼時兩邊要對得上，擋掉 CSRF 與
把 `id_token` 拿去重放。`id_token` 是我們自己用 client secret 直接向 Google 的
token endpoint 換來的（TLS + 憑證驗證），依 OIDC Core 3.1.3.7 這種情形不必另外驗簽；
但 `iss` / `aud` / `exp` / `nonce` / `email_verified` 一項都不會少。
登入後的轉址目標只收單斜線開頭的站內路徑，`//host` 這類開放轉址會被丟掉。

**記錄**：註冊當下與最近一次登入的 IP、國家、城市、User-Agent 都寫在帳號上，
完整歷程在稽核表（`GET /api/audit/logins`）。位置來自 Cloudflare 標頭，
要在後台開 Managed Transforms 才會送。

### 使用者資料存哪裡：SQLite 還是 MSSQL

預設存本機 SQLite（`data/library.db` 的 `app_user`）。填了下面這幾項就改存
MSSQL 的 `dbo.filmax_users`：

```ini
MSSQL_HOST=你的資料庫位址
MSSQL_PORT=1433
MSSQL_DATABASE=filmax
MSSQL_USER=filmax_app
MSSQL_PASSWORD=
MSSQL_DRIVER=ODBC Driver 18 for SQL Server
MSSQL_TRUST_CERT=false        # 內網自簽憑證要設 true
```

**沒有「連不上就自動退回 SQLite」這種行為，而且是刻意的。** 那看起來很貼心，
實際上是把權限判斷變成兩個各說各話的來源：在 MSSQL 核准的人在 SQLite 不存在，
反過來也一樣，而且沒有人會發現自己正在用哪一份。設定了就是要用它，
連不上就明講連不上（登入頁會顯示 503，日誌與 `/api/diagnostics` 有原因）。

**接上去的步驟**：

1. Windows 安裝 **Microsoft ODBC Driver 18 for SQL Server**（不是 SQL Server 本身）
2. `pip install pyodbc`
3. 資料庫還沒建的話，用 sysadmin 帳號跑一次 `db/00_create_database.sql`
4. `db\migrate.bat` 套用 Flyway 遷移（V1～V5）
5. `.env` 填上面那幾項
6. **`db\檢查連線.bat`** —— 這一步不要跳過

第 6 步會逐項檢查驅動、連線、資料表與欄位、定序、實際的讀寫權限
（真的寫一筆再刪掉）、稽核表的 `action` 限制式、清理程序的執行權限，
並在每一關失敗時告訴你要修什麼。已經有本機帳號的話：

```
db\檢查連線.bat --migrate
```

會把 SQLite 的 `app_user` 搬進 MSSQL（同 email 的跳過不覆蓋），SQLite 的舊資料
刻意保留不刪，等你確認過再自己清。

> **Flyway 的 V4 原本會失敗。** V1／V2 後來補上了 V4 想加的那些欄位，
> 於是在全新的資料庫上 `ALTER TABLE ... ADD` 會撞到
> `Column names in each table must be unique`，Flyway 停在 V4，
> 而錯誤訊息看起來像是「Flyway 壞了」。現在 V4 每一句都包在
> `IF COL_LENGTH(...) IS NULL` 裡，舊資料庫照樣補得到欄位，新資料庫安靜通過。
> 修的是 V4 而不是回頭改 V1／V2 —— Flyway 記了每個檔案的 checksum，
> 只要在任何一台機器套用過，事後修改就會讓 `validate` 失敗。

**兩張表**：`dbo.filmax_users`（帳號、角色、狀態、註冊與最近登入的來源）與
`dbo.filmax_audit`（每一筆事件的完整來源）。程式只讀寫資料，**不會建表也不會改表** ——
服務用的 `filmax_app` 帳號被明確 `DENY` 掉 DDL，結構一律走 Flyway。

**時間**一律以 epoch 秒在程式裡流動；MSSQL 存 `DATETIME2`（UTC），
換算集中在 `app/mssql.py`。這是最容易出錯的地方：`DATETIME2` 讀回來是**沒有時區**的
`datetime`，直接 `.timestamp()` 在 UTC+8 會整整差 8 小時。

### 遠端自動降畫質

家用光纖的**上傳**頻寬通常遠小於下載，直接把 4K 原檔推出去一定會卡。
程式依來源 IP 自動判斷：

```ini
REMOTE_MAX_HEIGHT=720       # 遠端最高解析度
REMOTE_BITRATE_KBPS=2800    # 遠端峰值位元率上限
LAN_BITRATE_KBPS=0          # 區網不鎖
LOCAL_NETWORKS=             # 額外算「本地」的網段
AUDIO_CHANNELS=2            # 轉碼輸出聲道：2=降混立體聲(預設)、6=保留5.1、0=不動原始
AUDIO_BITRATE_KBPS=192      # 轉碼音訊位元率
COOKIE_SECURE=false         # 對外開放時務必設 true
```

### 對外開放前的檢查清單

這個服務原本是設計在區網用的。要放上公開網域之前，以下每一項都要確認：

| 項目 | 設定 |
|---|---|
| 密碼 | `AUTH_PASSWORD` 換成夠長的隨機字串；要分享看片就另設 `VIEWER_PASSWORD` |
| Google 登入 | 用 `GOOGLE_ADMIN_EMAILS` 指定第一個管理員；`GOOGLE_AUTO_APPROVE` 維持 `false` |
| 簽章金鑰 | 設 `AUTH_SECRET`，或確認 `data/secret.key` 存在且權限收好 |
| Cookie | `COOKIE_SECURE=true` |
| 代理標頭 | 前面真的有 cloudflared / nginx 才設 `TRUST_PROXY=true` |
| 綁定位址 | `HOST=127.0.0.1`，只讓同機的 cloudflared 連得到，不要開 `0.0.0.0` |
| 開發模式 | `DEV_RELOAD=false` |
| API 文件 | 考慮關掉 `/api/docs` |

### 兩種角色

角色有兩種，跟你用哪種方式登入無關：密碼登入時由 `AUTH_PASSWORD` / `VIEWER_PASSWORD`
決定，Google 登入時由帳號上的 `role` 決定（`owner` = 管理員、`viewer` = 唯讀）。

| | 管理員（`AUTH_PASSWORD` / `owner`） | 唯讀（`VIEWER_PASSWORD` / `viewer`） |
|---|---|---|
| 瀏覽片庫、播放、字幕、續看進度 | ✅ | ✅ |
| 下載原始檔 | ✅ | ❌ |
| 瀏覽 FTP 目錄 | ✅ | ❌ |
| 掃描、重新分析、重新刮削、清快取 | ✅ | ❌ |
| 診斷資訊（FTP 位址、片庫路徑、硬體） | ✅ | ❌ |
| 使用者審核與角色調整 | ✅ | ❌ |
| 播放器設定（字幕外觀等） | 各自一份 | 各自一份 |

要分享給別人看片，就給 `VIEWER_PASSWORD` 那組，或請對方用 Google 帳號申請再核准。
`VIEWER_PASSWORD` 留空就是不開放唯讀密碼登入。

角色是寫進 session token 並納入簽章的，改不了也偽造不了。介面上會隱藏唯讀
使用者用不到的按鈕，但真正的把關在後端 —— 直接打 API 一樣會被擋（403）。

**撤銷 session**：改掉某一組密碼，用那組密碼登入的既有 session 會自動失效
（密碼指紋有納入簽章）；Google 帳號則是停權或改角色就立刻失效。
不想動任何設定但要把所有人一次踢掉，用管理員身分打
`POST /api/session/revoke-all`。

服務本身會送出這些安全標頭：CSP、`X-Frame-Options: DENY`、`X-Content-Type-Options`、
`Referrer-Policy`、`Permissions-Policy`，`COOKIE_SECURE=true` 時另外加上 HSTS。

### 音軌與字幕

多音軌的片子可以在播放器設定面板切換音軌，選過的會記在那部片上，下次開自動沿用。
指定音軌只有轉碼播放做得到（直接播放是把原始檔整個交給瀏覽器，選哪一軌由它決定），
所以切音軌時會自動改用轉碼。

字幕預設自動挑繁體中文：先看語言標籤，標籤分不出繁簡時再看實際內容用字判斷，
發現是簡體會自動換下一個中文軌。手動選過就記住，之後不會被自動判斷推翻。

PGS、VobSub 這類**圖形字幕**是一張張圖片而不是文字，沒辦法轉成網頁字幕，
面板上會標成灰色並註明無法顯示。要看的話只能下載原檔用本機播放器。

> 這個版本新增了 fps 等欄位。既有的檔案要重新分析過才會有 —— 在片庫按「重新掃描」
> 並勾選完整掃描，或對單一檔案重新分析即可。沒重新分析也不影響播放，
> 只是資訊面板那幾欄會空著。

區網（`10./172.16./192.168.`、loopback）算本地，其餘算遠端並自動降到 720p，
播放頁會跳提示告訴使用者。使用者也能在播放器的設定面板手動選畫質。

> **用 Tailscale 的話注意**：它的 `100.64.0.0/10` 在 Python 眼中不算私有網段，
> 預設會被判成遠端。想維持原畫質就設 `LOCAL_NETWORKS=100.64.0.0/10`。

### 對外開放：Cloudflare Tunnel

想用自己的網域對外開放，走 **Cloudflare Tunnel** 是最省事也最安全的做法：
不必在路由器開任何連接埠，你的機器在公網上沒有可掃到的入口，
HTTPS 憑證與 DDoS 防護都由 Cloudflare 處理。

設定方式看 [`cloudflare/README.md`](cloudflare/README.md)。有兩條路：
從 Cloudflare 後台建立通道再拿權杖回來安裝（建議，不必跑瀏覽器授權），
或用 `cloudflared tunnel login` 從指令列建立。兩個腳本都在 `cloudflare/`。

跑完之後 `.env` 一定要改：

```ini
HOST=127.0.0.1        # 只讓同機的 cloudflared 連得到
TRUST_PROXY=true      # 前面確實有代理時才開
COOKIE_SECURE=true
```

`HOST` 留在 `0.0.0.0` 的話，區網裡的人仍可繞過 Cloudflare 直連，
那條路徑沒有 WAF 也沒有 HTTPS。

**沒開驗證時，外部連線會被直接擋掉。** `AUTH_ENABLED=false` 的情況下，
只有來自區網／本機的連線可以使用（維持原本的區網用法），
從外部進來的一律回 403 並說明原因。這是為了避免接上 Tunnel 或反向代理
卻忘了打開驗證——那會讓整個媒體庫對外公開，而且每個訪客都是管理員。
`/healthz` 不受影響，監控不會誤判。

### 登入紀錄

每次登入（成功、失敗、被鎖）都會寫進本機的 `login_audit`，含來源 IP、
國家／地區／城市、時區、經緯度與 User-Agent。管理員可以打
`GET /api/audit/logins` 查看。

位置資訊來自 Cloudflare 的標頭，要在後台開
**Rules → Managed Transforms → Add visitor location headers** 才會送。
沒開的話只有國家，其餘顯示「位置未知」——那不是壞掉。

> 經緯度是**城市中心點**，不是使用者的實際位置，精度大概到城市等級。

### 不想公開網址的話

**Tailscale / WireGuard** 仍然是最單純的選擇 —— 零對外攻擊面。
用 Tailscale 時記得設 `LOCAL_NETWORKS=100.64.0.0/10`，否則會被判成遠端而降畫質。

---

## 9. 相片庫

掃描時會把 `LIBRARY_ROOTS` 底下的圖片（jpg / jpeg / png / webp）一併收進來，
在首頁上方切到「相片」分頁瀏覽。依資料夾分組，點縮圖開燈箱看大圖，
`←` `→` 翻頁、`Esc` 關閉。

每張顯示：尺寸、格式、色彩模式、檔案大小、修改時間，以及 EXIF 裡的
拍攝時間、相機、鏡頭、快門、光圈、ISO、焦距。沒有 EXIF 的圖會明講
「這張圖沒有 EXIF」，而不是留一片空白。

幾個實作上的決定：

**不讀 GPS。** EXIF 的 GPSInfo 是拍攝者的實際座標，精度到公尺等級 ——
相片一旦分享出去就等於公開了住家或行蹤。程式用**白名單**只讀需要的欄位，
而不是「全讀再刪掉 GPS」：白名單漏掉東西只是少一格資訊，
黑名單漏掉就是把座標寫進資料庫。原始檔案不會被修改，只是不讀進來。

**尺寸回報的是「看到的」而不是「存的」。** 手機直式照片常常是以橫式像素
加上 EXIF orientation 旗標存下來的。照原始像素顯示的話，介面會說 1200×800、
使用者看到的卻是 800×1200。縮圖也會依 orientation 轉正。

**圖片的大小門檻跟影片分開。** `MIN_FILE_MB` 預設 50MB 是為了濾掉預告片，
套在照片上會把幾乎所有圖片濾掉，所以圖片改用 `MIN_PHOTO_KB`（預設 40KB）。

**影片的海報不算相片。** 影片資料夾裡的 `poster.jpg`、`fanart.jpg`、`cover.png`
這類固定檔名，以及跟同資料夾影片同名的圖（`玩命關頭.mkv` 旁邊的 `玩命關頭.jpg`），
掃描時會跳過。不跳的話點「相片」會看到一整牆的影片封面，
真正的照片被埋在裡面。要全部收進來就把 `PHOTO_EXCLUDE_VIDEO_ARTWORK` 設 false。

**排序看的是拍攝時間，不是掃描時間。** 相片牆依 `sort_ts` 由新到舊排 ——
有 EXIF 拍攝時間就用它，沒有就退回檔案修改時間。這些時間在資料庫裡存的是
**epoch 整數**而不是字串：FTP 伺服器回的時間格式有五種以上
（MLSD 的 ISO、EXIF 的 `2024:03:09 14:05:22`、LIST 的 `Mar 15 10:22`
與沒有年份的 `Mar 15  2024`、DOS 的 `03-15-24 10:22AM`），
存字串就等於用字典序排時間，同一天內的順序會亂掉。
解析集中在 `app/timeparse.py`，認不出來的一律留空而不是猜一個。

> 唯讀角色看得到相片，但沒有「下載原圖」按鈕。要注意的是，
> **看大圖本身就是在取得原始檔案** —— 相片不像影片可以只給轉碼串流，
> 所以這個限制對相片來說主要是介面上的提示，不是真正的技術隔離。

---

## 10. 資料層：批次寫入與刪除

**寫入是批次的。** 原本掃描時每個檔案一句 `db.execute()`，而每一句都是一個
獨立交易 —— WAL 底下就是一次 fsync。109 個影片檔加 679 張相片就是快 800 次。
現在走訪期間的寫入全部進緩衝，累積到 500 筆才一起送出。實測（真的 FTP 伺服器、
上面那個規模）：**首次掃描 784 → 125 個交易，重掃 784 → 5 個。**

不是每張表都適合批次：

| 表 | 批次 | 為什麼 |
|---|---|---|
| `media_file` / `photo` | 可以 | 純 upsert，寫完不需要當場拿到 id |
| `media_item` / `episode` | **不批次** | 要先有 `item_id` 才寫得了檔案列 |

條目改用行程內註冊表：掃描開始時把既有的 `guess_key → id` 全部載進記憶體，
之後在記憶體裡查，新建的順手記下來。省掉的是每個檔案一次 `SELECT`。
硬把條目做成批次只會製造難修的競態 —— 兩個地方同時建立同一個 `guess_key`，
同一部影集就變成兩張海報，一張 12 集一張 1 集。

**刪除只有一個進入點。** `app/purge.py` 的 `purge()` 是唯一會刪媒體庫資料的地方，
`reason` 必填而且會進 `purge_log`。SQLite 其實有外鍵也有 `ON DELETE CASCADE`，
但外鍵管不到兩件事，而那兩件正是實際會出問題的：

- **磁碟上的檔案。** 海報、底圖、縮圖都是 `data/images/` 底下的真檔案。
  資料列被 CASCADE 掉之後，檔案就永遠留在那裡，沒有任何東西記得它存在。
- **換一個後端就不一樣了。** 規格 A-5 的 MSSQL 媒體庫刻意不做外鍵。
  刪除邏輯藏在 CASCADE 裡的話，換後端等於整套刪除行為靜默改變。

相片縮圖是**內容雜湊**命名的（見 §9），所以兩張一模一樣的照片會共用同一個檔案。
刪掉其中一張就順手刪檔，另一張會變破圖 —— 所以刪檔前一定要數參照。

**孤兒清掃**分三個時機，模式不一樣：

| 時機 | 模式 | 為什麼 |
|---|---|---|
| 掃描的 `finally` | 修 | 「被取消」與「出錯」正是孤兒最多的結局，只掛成功路徑等於永遠不清最需要清的那次 |
| 服務啟動 | 只回報 | **刻意不修**。啟動路徑自動刪資料很糟，尤其剛還原備份或剛換後端之後 |
| `POST /api/maintenance/sweep` | 兩種 | 預設 `report`，先看清單再決定 |

第 5、6、7 類孤兒（沒有檔案的條目／集數、沒人參照的圖片）有 **1 小時寬限期**：
索引流程是「先建條目、再寫檔案列」，兩步之間那個條目是完全合法的孤兒。

孤兒計數露在 `/api/stats`。沒有外鍵的資料庫，這個數字就是體溫計 ——
它持續往上，代表刪除路徑有地方漏了。

---

## 11. 管理後台與系統參數

`/admin`，只有管理員進得去，手機可用。六個分區：

| 分區 | 有什麼 |
|---|---|
| 總覽 | 執行時間、影片／相片／容量計數、磁碟剩餘、孤兒計數、最近 10 筆活動、進行中的掃描、問題警示 |
| 媒體庫 | 掃描控制與即時進度（含相片階段）、未刮削清單、失敗檔案與錯誤、FTP 目錄瀏覽器、孤兒清掃 |
| 使用者 | 清單與狀態篩選、核准／拒絕／停權／改角色／刪除、備註、最後登入來源 |
| 播放與轉碼 | ffmpeg 狀態、硬體加速探測、編碼器深度診斷、轉碼實測、FTP 併發量測、GPU、清快取 |
| 系統 | 資料庫狀態、FTP 測試、TMDB 狀態、系統參數設定 |
| 紀錄 | 登入稽核（伺服器端分頁）、掃描日誌、設定變更稽核 |

`/admin` 這條路由**自己檢查角色** —— 中介層只驗「有沒有登入」，不驗「是不是管理員」。
`admin.js` 放在 `/static/` 底下，不需登入就下載得到，所以裡面沒有任何敏感資訊；
真正的保護在每一個 API 端點上。

手機版斷點 768px。表格換成卡片，**不是**用 CSS 把欄位藏起來 ——
藏起來等於資料消失而且沒有替代入口。iOS 刻意不做邊緣滑動開啟抽屜，
那會跟系統的返回手勢打架。

### 系統參數：`.env` 與資料庫怎麼共存

兩邊比大小的話，兩個方向各有一種很惡毒的失效模式：

| 誰贏 | 失效模式 |
|---|---|
| 資料庫贏 | **改 `.env` 沒反應。** `.env` 是自架使用者唯一熟悉的介面，變成裝飾品之後使用者會認定程式壞了。輪替金鑰時只改 `.env`、以為換好了、實際仍用舊值 —— 那是安全事故等級 |
| `.env` 贏 | **存了但沒生效。** 在後台改值、按存檔、看到成功提示、行為完全沒變，也沒有警告 |

選 **`.env` 贏**，因為它的失效模式是純 UX 問題，而 UX 問題可以用 UI 解決。
所以解法不是比大小，是**把「誰在管這一項」變成畫面上看得見的東西**：

```
.env / 環境變數  >  資料庫  >  程式內建預設值
```

一條規則，沒有例外清單。API 不只回生效值，還回三個候選值與兩個旗標：

```json
{ "key": "TRANSCODE_CRF",
  "effective": { "value": 21, "source": "env" },
  "candidates": { "env": 21, "db": 23, "default": 21 },
  "locked": true, "shadowed": true, "applyMode": "hot", "secret": false }
```

`shadowed` 一個欄位就解決「`.env` 改了但資料庫已經有值」的全部歧義。
被鎖住的欄位在後台是**停用**的，旁邊寫明「此項由環境變數設定」，
而且不給儲存鈕 —— 停用的欄位絕不能是「送出之後才在後端報錯」。

### 哪些設定放得進後台

分層是四個機械化問句問出來的，不是憑感覺：

| 問句 | 答「是」 | 例 |
|---|---|---|
| 改錯了會不會讓我連後台都進不去？（或讀它時資料庫還沒連上） | 只能放 `.env` | MSSQL 連線、`HOST`/`PORT`、`AUTH_SECRET`、`USER_STORE`/`MEDIA_STORE` |
| 這個值會被當命令、路徑或程式碼執行嗎？ | 不放 UI | `FFMPEG_PATH`、`LIBRARY_ROOTS`、`TRUST_PROXY`、`LOCAL_NETWORKS` |
| 洩漏了是外部系統倒楣嗎？ | 秘密（唯寫） | `TMDB_API_KEY`、`FTP_PASSWORD`、`GOOGLE_CLIENT_SECRET` |
| 啟動時被拿去建立長生命週期的東西了嗎？ | 需重啟／重載 | 探測併發數、快取上限、FTP 連線資訊 |
| 以上都不是 | **後台可改，即時生效** | 掃描排除、轉碼參數、遠端畫質、相片門檻、TMDB 語言 |

> Tier 4 有先例：Navidrome 把轉碼設定的 UI 預設關閉，理由寫在他們的安全文件裡 ——
> 「它讓攻擊者可以在你的伺服器上執行任意命令」。`FFMPEG_PATH` 是同一類東西。

**「秘密」跟分層是獨立的兩件事。** 分層講的是「可以在哪裡設定」，秘密講的是
「可不可以讀出來」。`AUTH_PASSWORD` 只能放 `.env`（改錯了會把自己鎖在外面），
但它同時是不折不扣的密碼。一開始把秘密寫成分層的衍生屬性，結果就是 CLI
把 `AUTH_PASSWORD` 明文印在畫面上。

### 即時生效是怎麼做到的

即時生效的那些設定**不是 dataclass 欄位，是每次讀都重新解析的 property**。
dataclass 欄位在 import 時就定案了 —— 後台改了值、存進資料庫、UI 顯示「已生效」，
而程式仍然在用啟動當下讀到的那個數字。

同樣的道理，**任何模組都不可以在頂層寫 `CRF = settings.crf`**。那等於又把它
凍回 import 當下的值，而 UI 仍然顯示「已生效」。這種退步不會出任何錯，
只是沒有作用，所以 `tests/phase3_test.py` 有一項用 AST 掃過每個模組的頂層釘住它。

### 其他機制

- **啟動時漂移掃描** —— 被環境變數蓋掉的後台設定會在日誌與後台橫幅上列出來，
  但**絕不自動刪**：使用者可能只是暫時用環境變數覆蓋（測試、容器編排）。
- **`.env` 的錯字抓得出來。** `TMDB_API_KAY=xxx` 本來是完全靜默失效的，
  現在會說「是不是想打 `TMDB_API_KEY`？」
- **大小寫與別名不算「設錯了」。** `HDR_TONEMAP=AUTO` 與 `FTP_ENCODING=cp950`
  都收得下 —— 升級之後因為大小寫而開不起來，對使用者來說就是「你們改壞了」，
  而他什麼都沒動。編碼名稱刻意不列白名單，改成問 Python 認不認得。
- **驗證嚴格度兩邊不同，這是刻意的。** `.env` 的值無效 → **啟動失敗**
  （那是使用者明確寫下的指令，錯了還默默跑起來他會以為生效了）；
  資料庫的值無效 → 退回預設並在後台標示（一個舊版留下的壞值不該讓機器開不了機）。
- **秘密**存資料庫時加密，密文帶固定前綴，加密金鑰本身只能放 `.env`。
  支援 `XXX_FILE` 從檔案讀。日誌過濾掛成 **logger filter** 而不是在每個呼叫點
  各自處理 —— 逐點處理只能防住你想得到的那些點。
- **設定變更稽核**：誰、何時、哪一項、從什麼改成什麼。
  秘密只記「已變更」，**連遮罩後的值都不記** —— 長度也是情報。
- **CLI 救援路徑**，後台進不去時唯一的出路：

```
python -m app.paramstore list [區段]
python -m app.paramstore get TRANSCODE_CRF
python -m app.paramstore set TRANSCODE_CRF 20
python -m app.paramstore unset TRANSCODE_CRF
python -m app.paramstore diff          # 只列跟預設值不同的
```

---

## 12. 播放器

自製播放器，沒有用瀏覽器原生控制列。字幕**不是**交給 `<track>`，
而是自己解析 WebVTT 後用 DOM 畫出來 —— `::cue` 的跨瀏覽器支援很差，
自己畫才能真的控制大小、顏色與位置。

字幕可調：**大小、顏色、位置（上/下 + 距離邊緣）、背景、邊緣描邊、字體、粗細、延遲**。
延遲那項對 FTP 抓來的片子特別實用，字幕對不上時 `G` / `H` 就能即時微調 ±0.1 秒。

**設定會記住。** 先寫進瀏覽器（立即生效、離線也不會掉），過幾秒同步到伺服器，
換一台裝置登入就沿用。時間戳一律由伺服器蓋章 —— 只要有一台裝置時鐘不準，
用它的時間排序就會讓其他裝置的設定永遠同步不上去。

**快轉秒數**可選 3 / 5 / 10 / 30 秒。

### 全螢幕：手機上有兩種，而且 iPhone 只有一種

桌機按 `F` 就是瀏覽器全螢幕，沒什麼好說的。手機是另一回事：

- **iOS Safari 沒有 `Element.requestFullscreen`**。整個播放器容器要全螢幕
  在 iPhone 上是做不到的 —— 那支 API 根本不存在，呼叫下去只會拿到
  `undefined is not a function`。iPhone 唯一能全螢幕的是 `<video>` 元素本身，
  透過 `video.webkitEnterFullscreen()`。
- 但走原生全螢幕就等於把畫面交給系統播放器，**自繪字幕會消失**
  （它是 DOM，不在 video 裡）。

所以設定面板給兩個模式，預設**自動**：

| 模式 | 做什麼 | 字幕 |
|---|---|---|
| 網頁全螢幕 | 播放器容器撐滿可視區域（`100dvh` + 安全區內距），控制列與自繪字幕全部保留 | 自繪，全部設定都有效 |
| 原生全螢幕 | 交給系統播放器，可以轉到橫向、狀態列會收起來 | 自動塞進 `<video>` 的字幕軌，樣式由系統決定 |

面板顯示的是**實際會發生的那一個**，不是設定值。挑到瀏覽器做不到的模式時
會自動退回另一種，而面板上跟著改 —— 顯示「原生」但實際跑網頁全螢幕
是最難查的那種 bug。

進原生全螢幕時，自繪字幕會即時餵進 `<video>` 的 TextTrack（去掉 HTML 標籤），
退出時關掉並還原自繪。切換字幕軌會清掉舊的句子 —— 這裡有個坑：
`TextTrack.cues` 在 `mode === 'disabled'` 時是 `null`，
用 `disabled` 藏字幕的話清除迴圈會安靜地什麼都沒做，中英文字幕就疊在一起了。
所以平常是 `'hidden'`（有在跑但不顯示）而不是 `'disabled'`。

### 換頁不會跳回最上面

海報牆換頁時捲到**格線頂端**，不是文件頂端 —— 捲到文件頂端會停在標頭、
搜尋列、分類標籤之上，離第一列還有一大段，等於每次換頁都要再往下捲一次。
sticky 標頭的高度是動態量的，不是寫死：手機上標頭會換行，高度跟桌機不一樣。

本來就在最上面的話就不捲。這裡有個不明顯的地方：**這個判斷必須在換內容之前做**。
換頁時格線會先被清成「載入中」，頁面高度瞬間塌掉，瀏覽器把捲動位置夾到 0 ——
這時候才問「使用者是不是已經在上面了」，答案永遠是「是」，於是永遠不捲。

### 音軌

多音軌的片子可以直接在設定面板切換，選過的記在那部片上，下次開自動沿用。
指定音軌只有轉碼播放做得到（直接播放是把原始檔整個交給瀏覽器，選哪一軌由它決定），
所以切音軌時會自動改用轉碼。切畫質不會把選好的音軌洗掉。

轉碼預設把聲道降混成立體聲（`AUDIO_CHANNELS=2`），因為多聲道 AAC 包在 mpegts 裡
各家瀏覽器支援度並不一致。面板上會註明「轉碼後降混成立體聲」，不會讓 5.1 的標籤說謊。

快捷鍵：

| 鍵 | 功能 | 鍵 | 功能 |
|---|---|---|---|
| `空白` / `K` | 播放 / 暫停 | `C` | 字幕開關 |
| `←` `→` | ±10 秒（`Shift` 為 ±60 秒） | `G` `H` | 字幕延遲 ∓0.1 秒 |
| `↑` `↓` | 音量 | `F` | 全螢幕 |
| `M` | 靜音 | `Esc` | 關閉設定面板 |

---

## 13. 端對端測試 (e2e_test.py)

服務啟動後，隨時可以跑一遍完整鏈路檢查，逐層告訴你問題出在哪一環：

```
.venv\Scripts\python e2e_test.py
```

它會依序驗證：**ffmpeg / ffprobe → 服務 → FTP → 索引 → 格式分析 → Range 串流
（含中段 seek）→ HLS 分段轉碼（含跳段）→ 字幕轉檔**，每段轉碼還會回報
「6 秒影片花了幾秒轉完」，低於 1x 即時速度會警告你播放會卡。

常用參數：

```
python e2e_test.py --file-id 42        # 只測某個檔案（詳情頁看得到 id）
python e2e_test.py --samples 5         # 多挑幾個檔案測
python e2e_test.py --skip-transcode    # 跳過最花時間的轉碼測試
python e2e_test.py --base http://192.168.1.5:8080
```

只用 Python 標準函式庫，不需要額外套件。網頁右上角 **ⓘ** 也有同一份診斷的摘要版。

### Google 登入測試 (tests/oauth_test.py)

登入流程沒辦法靠肉眼看出「安全」，所以另外有一份不連 Google 的端對端測試：

```
.venv\Scripts\python tests\oauth_test.py
```

它會用暫存資料夾當資料庫（跑完就刪，不會動到你的 `data/`），自己扮演 Google
回傳 `id_token`，然後驗 58 項：完整的申請→審核→登入流程、停權與降級是否立刻生效、
最後一個管理員的保護，以及 `state` 不符／`nonce` 重放／`aud` 不對／`iss` 造假／
`id_token` 過期／email 未驗證／竄改 state cookie／開放轉址／畸形輸入不能變成 500。

同一套測試可以切到 MSSQL 那條程式碼路徑：

```
.venv\Scripts\python tests\oauth_test.py --mssql
.venv\Scripts\python tests\mssql_test.py
```

後面接的是 `tests/fake_pyodbc.py`（把 pyodbc 換掉，底下用 SQLite），
所以**不需要真的 SQL Server**。它驗得到的是 SQL 欄位對齊、參數順序、
時間轉換、欄位長度截斷、斷線時不會變成 500、密碼不會漏進日誌。

它**驗不到**的是方言層面的東西：定序、`DATETIME2` 精度、`OUTPUT` 在有觸發程序時的
行為、實際權限。那些要在真的資料庫上跑 `db\檢查連線.bat`。這個分工是刻意的 ——
與其寫一套「看起來全綠但其實沒碰到真資料庫」的測試，不如把驗不到的部分交給
一支你在自己機器上跑得動的工具。

### 其他測試

全部都不需要外部服務，跑完就刪暫存資料夾，不會動到你的 `data/`。

```
.venv\Scripts\python tests\phase1_test.py       # 分組規則、時間解析、相片過濾、靜態檢查
.venv\Scripts\python tests\phase2_test.py       # 批次寫入、purge／sweep、時間值域、取消
.venv\Scripts\python tests\phase3_test.py       # 設定分層與解析、秘密、後台權限
.venv\Scripts\python tests\scan_e2e_test.py     # 開一台真的 FTP 伺服器掃一遍
.venv\Scripts\python tests\sql_params_test.py   # SQL 佔位符與參數數量對不對
.venv\Scripts\python tests\env_drift_test.py    # 程式讀的設定 ↔ .env.example
.venv\Scripts\python tests\browser_test.py      # 開真的 Chromium 按按看（需要 playwright）
```

後三支是為了擋掉**同一類**的錯，而不是為了某一個 bug：

- `sql_params_test.py` 用 AST 數每一句 SQL 的 `?` 跟傳進去的參數個數。
  這類錯只有在該條路徑真的被走到時才爆，而 `UPDATE` 分支往往要第二次掃描才進得去 ——
  第一次掃描的測試全綠，錯還在裡面。
- `env_drift_test.py` 比對 `config.py` 讀的鍵跟 `.env.example` 寫的鍵。
  加了設定忘了寫進範例檔，使用者永遠不知道它存在；範例檔留著早就刪掉的鍵，
  使用者照著設卻毫無作用 —— 兩種都不會有任何錯誤訊息。
- `browser_test.py` 開真的瀏覽器。靜態檢查只能證明「程式碼改了」，
  證明不了「畫面會動」；捲動與全螢幕這兩件事，除了真的按下去以外沒有別的驗法。

> `browser_test.py` 裡的分頁鈕是用 JS 觸發 click 的，不是 Playwright 的
> `page.click()`。因為 `page.click()` 會**先把元素捲進視窗**才按 ——
> 分頁鈕在第一屏之外，於是每次點擊當下的捲動位置都是 Playwright 捲過去的那個，
> 不是測試設定的那個。那樣量到的是 Playwright 的行為，不是 app 的。

---

## 14. 疑難排解

**播放時出現「分析失敗：[WinError 2] 系統找不到指定的檔案」**
→ 這是找不到 `ffprobe`。注意 **ffmpeg 和 ffprobe 是兩支獨立的執行檔**，
有些安裝方式（例如 winget 的 shim）只會把 `ffmpeg.exe` 掛上 PATH，漏掉 `ffprobe.exe`。
程式現在會自動去 winget / chocolatey / scoop 的套件目錄找，找不到才報錯；
真的找不到就在 `.env` 把 `FFPROBE_PATH` 設成完整路徑，例如：

```ini
FFPROBE_PATH=C:\Users\你\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_xxx\ffmpeg-7.1-full_build\bin\ffprobe.exe
```

**只有 ffmpeg、沒有 ffprobe 也能動。** 程式會退回「解析 `ffmpeg -i` 輸出」的備援方式取得
影片資訊，播放完全正常，網頁 ⓘ 會用黃字提醒你目前在備援模式。不過還是建議裝完整版：

```
winget install Gyan.FFmpeg
```

裝完把 `.env` 的 `FFMPEG_PATH` / `FFPROBE_PATH` 留空（或設成新的 bin 路徑），重新啟動即可。
程式會**優先挑同時有 ffmpeg 和 ffprobe 的目錄**，避免用到別的軟體夾帶的舊版 ffmpeg。

修好之後按一次「掃描媒體庫」，之前分析失敗的檔案會自動重試。


**中文檔名變亂碼** → `.env` 的 `FTP_ENCODING` 改 `gbk`（簡體來源）或 `big5`，重啟後重新掃描。

**掃描時列不到目錄** → 先用 `http://localhost:8080/api/ftp/browse?path=/` 確認路徑對不對，
再檢查 `LIBRARY_ROOTS` 是否從 FTP 根目錄算起。

**播放一直顯示「轉碼中」** → 看伺服器主控台有沒有 ffmpeg 錯誤；
最常見是 `FFMPEG_PATH` 沒設好，或 FTP 伺服器限制了同時連線數
（每個轉碼分段會開一條 FTP 連線，加上預轉會同時開 3～4 條，可以把 `HLS_PREFETCH_SEGMENTS` 調成 1）。

**詳情頁顯示「未刮削」** → 通常是檔名太乾淨或太亂導致搜不到，用「手動指定 TMDB」修正最快。

**想讓區網其他裝置連進來** → `.env` 的 `HOST` 保持 `0.0.0.0`，
Windows 防火牆放行該連接埠，其他裝置開 `http://你的電腦IP:8080`。

---

## 開發進度

本專案**持續開發中**，以下是目前的狀態。已完成的項目仍可能因為後續重構而調整。

### 已完成

- **媒體庫** — FTP 遞迴掃描、檔名解析（中英片名、年份、季集、動畫集數、一集一資料夾、檔名結尾數字）、TMDB 刮削、海報牆、分類與搜尋、繼續觀看、不重掃 FTP 的重新分組
- **播放** — 直接串流（HTTP Range）與隨選分段 HLS 轉碼自動選擇、HDR→SDR tonemap、H.264 level 自動選擇、遠端自動降畫質、轉碼快取與預轉
- **播放器** — 自製控制列、字幕自繪（大小/顏色/位置/描邊/字體/延遲）、多音軌切換、畫質切換、片源資訊、設定跨裝置同步
- **字幕** — 內嵌與外掛字幕轉 WebVTT、**邊抽邊送**（大檔案第一句字幕約 0.3 秒出現，而非等整部片 demux 完）、抽好存快取、**自動挑繁體中文**（先看語言標籤，標籤分不出繁簡時看實際內容用字判斷）、圖形字幕（PGS/VobSub）標示為無法顯示
- **相片庫** — 掃描圖片、讀尺寸與 EXIF、產縮圖、相片牆與燈箱檢視、依拍攝時間排序、排除影片海報
- **帳號** — Google 登入（authorization code + PKCE）、審核制註冊、後台審核介面、註冊與登入的 IP 與地理位置記錄
- **使用者資料** — 可存本機 SQLite 或 MSSQL（`dbo.filmax_users`），Flyway 管結構，附連線與權限的逐項檢查工具、SQLite→MSSQL 搬移
- **稽核** — 註冊／登入／被拒／核准／停權／改角色都留紀錄，本機一定寫，MSSQL 有設就一起寫
- **權限** — 管理員／唯讀兩種角色，角色寫進 session token 並納入簽章；改密碼或停權／降級都會讓既有 session 立刻失效
- **安全** — 內部憑證走標頭不走網址、來源 IP 判斷不採信可偽造的標頭、CSP 等安全標頭、輸入驗證與速率限制
- **對外開放** — Cloudflare Tunnel 一鍵設定（含 Windows 服務）、登入紀錄含來源 IP 與地理位置
- **管理後台** — `/admin` 六個分區，手機可用；系統參數同時支援 `.env` 與資料庫（`.env` 優先，逐欄位標示鎖定與遮蔽），秘密加密唯寫，設定變更稽核，CLI 救援路徑
- **資料層** — 掃描寫入批次化（重掃的交易數少兩個數量級）、單一刪除進入點含磁碟檔與稽核、孤兒清掃與計數、時間欄位一律 epoch 整數並有值域守門
- **維運** — 一鍵安裝腳本、端對端測試腳本（含 Google 登入的攻擊面測試）、診斷 API、MSSQL 資料表的 Flyway 遷移檔

### 進行中 / 規劃中

規格見 [`docs/規格需求書.md`](docs/規格需求書.md)。

- **媒體庫上 MSSQL**（規劃中）— 目前 `MEDIA_STORE` 預設且只支援 sqlite
- 每位使用者各自的播放進度（偏好設定已經是每人一份）
- Remux 直通（影像不轉碼只換容器），H.264 片源可望接近秒開

---

## 使用聲明與授權範圍

**這是一個媒體庫管理工具，不提供、不散布、也不協助取得任何影音內容。**
所有播放的檔案都來自使用者自己設定的 FTP 伺服器。

### 嚴禁用於非法用途

使用本專案即表示你同意，**不會**將它用於下列任何行為：

1. **散布或公開傳輸你不擁有合法權利的著作** —— 包含但不限於電影、影集、動畫、音樂
2. **規避、破解任何技術保護措施（DRM）**
3. **經營任何形式的盜版串流服務**，無論是否收費
4. **未經授權存取他人的 FTP 伺服器、資料庫或任何系統**
5. 任何違反你所在地或伺服器所在地法律的行為

### 你的責任

- 你必須對所管理的每一個影音檔案擁有**合法的持有與觀看權利**（自行拍攝、購買、或其他合法授權）
- 你必須對自己架設的服務負完全責任，包含存取控制、資料保護與法律遵循
- 對外開放前請先讀完[對外開放前的檢查清單](#對外開放前的檢查清單)

### 免責

本專案依「現狀」提供，**不附帶任何明示或默示的擔保**，包含但不限於適售性、
特定用途適用性與不侵權。作者與貢獻者**不對任何因使用或無法使用本軟體所生的
直接、間接、附帶、特殊、懲罰性或衍生性損害負責**，亦不對使用者的任何違法行為負責。

**使用者需自行承擔全部法律責任。** 若你所在地的法律不允許上述任一條件，請勿使用本專案。

---

## 貢獻與回報

問題回報與功能建議請開 [Issue](https://github.com/mark22013333/FilmaxWeb/issues)。

回報問題時附上這些會快很多：

- `GET /api/diagnostics` 的輸出（**注意**：裡面有 FTP 主機位址與片庫路徑，貼出來前先自行遮蔽）
- `python e2e_test.py` 的結果
- 伺服器主控台的 ffmpeg 錯誤訊息
- 有問題的檔案，`ffprobe -v error -show_streams` 的輸出

送 PR 前請先跑一次端對端測試。

**不要提交**：`.env`、`data/` 底下任何檔案、真實的主機位址或帳密。
`.gitignore` 已經擋掉前兩者，第三項請自己留意。
