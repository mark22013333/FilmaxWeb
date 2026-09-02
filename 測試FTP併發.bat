@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb 測試 FTP 併發上限

REM 量測你的 FTP 伺服器能承受幾條同時連線，以及開到第幾條之後就不再變快。
REM 結果會給一個 PHOTO_CONCURRENCY 的建議值。
REM
REM 這支只會登入、列目錄、下載一小段資料測速，不會寫入任何檔案。
REM 參數： --max 30    往上多試幾條
REM         --no-speed  只找上限，不測速度

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m app.ftpprobe %*
echo.
pause
