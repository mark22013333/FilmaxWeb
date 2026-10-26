@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title FilmaxWeb - Restart

if not exist ".env" (
  echo [!] 找不到 .env，請先執行啟動.bat 完成設定。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [!] 找不到 Python 環境，請先執行啟動.bat 完成安裝。
  pause
  exit /b 1
)

if not exist "run.py" (
  echo [!] 找不到 run.py，請確認腳本放在 FilmaxWeb 專案資料夾。
  pause
  exit /b 1
)

echo.
echo   === FilmaxWeb 重新啟動 ===
echo.

REM Redirect only the stop script so its pause does not interrupt the restart.
call "%~dp0停止.bat" <nul
if errorlevel 1 (
  echo [!] 停止失敗，已取消重新啟動，請查看上方訊息。
  pause
  exit /b 1
)

echo   正在重新啟動服務，原本的網頁重新整理即可。
echo.
".venv\Scripts\python.exe" run.py
set "RUN_EXIT=%ERRORLEVEL%"
if not "%RUN_EXIT%"=="0" echo [!] 服務已結束，錯誤代碼 %RUN_EXIT%，請查看上方訊息。
pause
exit /b %RUN_EXIT%
