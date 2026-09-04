@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title FilmaxWeb 環境安裝

echo.
echo ===============================================================
echo   FilmaxWeb 環境安裝
echo   可以重複執行，已經裝好的會自動跳過
echo ===============================================================
echo.

set NEED_RESTART=0
set FAILED=

REM ---------------------------------------------------------------- winget
where winget >nul 2>nul
if errorlevel 1 (
  echo [X] 找不到 winget。
  echo     請先從 Microsoft Store 安裝「應用程式安裝程式」後再執行一次。
  echo.
  pause & exit /b 1
)

REM ---------------------------------------------------------------- Python
echo [1/5] 檢查 Python...
where python >nul 2>nul
if errorlevel 1 (
  echo       找不到 python，正在安裝...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  set NEED_RESTART=1
) else (
  for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo       %%v
)

REM ---------------------------------------------------------------- ffmpeg
echo.
echo [2/5] 檢查 ffmpeg 與 ffprobe...
set HAVE_FFMPEG=0
set HAVE_FFPROBE=0
where ffmpeg  >nul 2>nul && set HAVE_FFMPEG=1
where ffprobe >nul 2>nul && set HAVE_FFPROBE=1

if "!HAVE_FFMPEG!!HAVE_FFPROBE!"=="11" (
  for /f "tokens=1-3" %%a in ('ffmpeg -version 2^>^&1 ^| findstr /b "ffmpeg version"') do echo       %%a %%b %%c
  echo       兩支都在。
) else (
  if "!HAVE_FFMPEG!"=="1" (
    echo       只找到 ffmpeg，缺 ffprobe ^(這正是之前分析失敗的原因^)
  ) else (
    echo       兩支都沒有
  )
  echo       正在安裝完整版 Gyan.FFmpeg...
  winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (set FAILED=!FAILED! ffmpeg) else (set NEED_RESTART=1)
)

REM ---------------------------------------------------------------- Python 套件
echo.
echo [3/5] 安裝 Python 套件...
if not exist ".venv\Scripts\python.exe" (
  echo       建立虛擬環境...
  python -m venv .venv
  if errorlevel 1 (
    echo       [X] 建立失敗。如果剛剛才裝好 Python，請關掉這個視窗重跑一次。
    set FAILED=!FAILED! venv
    goto :after_pip
  )
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo       [X] 套件安裝失敗
  set FAILED=!FAILED! pip
) else (
  echo       完成
)

REM Test-only packages. ASCII on purpose - see the note in the quality-probe
REM .bat files about cmd.exe parsing a .bat with the console code page.
REM pyftpdlib and playwright are needed by tests\scan_e2e_test.py and
REM tests\browser_test.py. They were never recorded anywhere, so on a clean
REM machine those two scripts just Traceback and it looks like the code broke.
echo.
echo       test packages (pyftpdlib / playwright)...
if exist "requirements-dev.txt" (
  ".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
  if errorlevel 1 (
    echo       [X] test packages failed
    set FAILED=!FAILED! pip-dev
  ) else (
    ".venv\Scripts\python.exe" -m playwright install chromium >nul 2>&1
    if errorlevel 1 (
      echo       [!] playwright browser download failed - browser_test will skip
    ) else (
      echo       done
    )
  )
)
:after_pip

REM ---------------------------------------------------------------- Flyway
echo.
echo [4/5] 檢查 Flyway ^(資料庫遷移用^)...
where flyway >nul 2>nul
if errorlevel 1 (
  echo       嘗試用 winget 安裝...
  winget install -e --id Redgate.Flyway --accept-source-agreements --accept-package-agreements >nul 2>nul
  where flyway >nul 2>nul
  if errorlevel 1 (
    echo       [!] winget 找不到這個套件，需要手動裝：
    echo           1. 到 https://documentation.red-gate.com/fd/command-line-184127404.html
    echo           2. 下載 flyway-commandline-*-windows-x64.zip ^(內含 JRE，不必另裝 Java^)
    echo           3. 解壓後把 flyway.cmd 所在資料夾加進 PATH
    echo           這一項只有第三期才會用到，現在沒有也不影響。
  ) else (
    echo       完成
    set NEED_RESTART=1
  )
) else (
  for /f "tokens=*" %%v in ('flyway --version 2^>^&1 ^| findstr /i flyway') do echo       %%v
)

REM ---------------------------------------------------------------- cloudflared
echo.
echo [5/5] 檢查 cloudflared ^(對外網址用，第三期^)...
where cloudflared >nul 2>nul
if errorlevel 1 (
  echo       尚未安裝。第三期要做 video.longhopick.com 時再裝即可：
  echo           winget install -e --id Cloudflare.cloudflared
) else (
  echo       已安裝
)

REM ---------------------------------------------------------------- 結果
echo.
echo ===============================================================
if not "!FAILED!"=="" (
  echo   [X] 這些項目失敗：!FAILED!
  echo       把上面的訊息貼給 Claude 看。
) else if "!NEED_RESTART!"=="1" (
  echo   有套件是剛剛才裝好的，PATH 還沒更新。
  echo.
  echo   請「關掉這個視窗」，然後重新執行一次本檔案做驗證。
) else (
  echo   全部就緒。
  echo.
  echo   接下來：
  echo     1. 確認 .env 的 FFMPEG_PATH / FFPROBE_PATH 填的是完整路徑
  echo        ^(不要留空、不要靠 PATH —— 這台機器上有 ImageMagick 附帶的
  echo         舊版 ffmpeg，靠 PATH 會拿到它。見 J 章第 3 步^)
  echo     2. 雙擊「啟動.bat」
  echo     3. 在網頁按一次「掃描媒體庫」，重新分析之前失敗的檔案
)
echo ===============================================================
echo.
pause
