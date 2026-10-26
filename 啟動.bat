@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FilmaxWeb

set "FIRST_RUN="
if not exist ".env" (
  echo [!TIME!] 第一次執行，建立空白的 .env ^(之後由網頁上的設定精靈幫你填^)
  > ".env" echo # FilmaxWeb 設定。第一次請在瀏覽器的「設定精靈」填寫，不用手動編輯這個檔案。
  >> ".env" echo # 所有可以調整的項目與說明都在 .env.example。
  set "FIRST_RUN=1"
)

if not exist ".venv\Scripts\python.exe" (
  echo [!TIME!] [1/3] 建立 Python 虛擬環境...
  python -m venv .venv || (
    echo 找不到 python，請先安裝 Python 3.10 以上並勾選 Add to PATH
    pause & exit /b
  )
)

echo [!TIME!] [2/3] 安裝相依套件...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (
  echo 套件安裝失敗
  pause & exit /b
)

where ffmpeg >nul 2>nul || echo [!] 警告: 找不到 ffmpeg，mkv/HEVC 等格式將無法轉碼播放。請安裝 ffmpeg 或在 .env 設定 FFMPEG_PATH。

REM 埠號直接從 .env 讀，不要寫死 —— 寫死的話改了設定就會開錯分頁。
REM findstr /B 只認行首，避免比對到註解裡提到 PORT 的那幾行。
set "PORT="
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /B /C:"PORT=" ".env"`) do set "PORT=%%b"
if not defined PORT set "PORT=8080"
REM 去掉可能的空白與行尾註解
for /f "tokens=1" %%p in ("!PORT!") do set "PORT=%%p"

REM 沒開驗證時服務只接受本機與區網連線，所以用 127.0.0.1 開最保險
echo [!TIME!] [3/3] 啟動服務  http://127.0.0.1:!PORT!
echo.
if defined FIRST_RUN (
  start "" http://127.0.0.1:!PORT!/setup
) else (
  start "" http://127.0.0.1:!PORT!
)
".venv\Scripts\python.exe" run.py
pause
