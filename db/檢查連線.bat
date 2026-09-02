@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title FilmaxWeb 資料庫連線檢查

REM 逐項檢查 MSSQL：驅動、連線、資料表、定序、讀寫權限、稽核限制式。
REM 帶 --migrate 會把本機 SQLite 既有的帳號搬進 MSSQL。
REM
REM 這支要在專案根目錄跑（上面的 cd 已經處理），因為它要讀 .env。

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m app.dbcheck %*
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
  echo 有項目沒通過，照上面的訊息處理後再跑一次這個檔案。
) else (
  echo 檢查完成。
)
pause
exit /b %RC%
