@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb 資料庫遷移

if "%FLYWAY_PASSWORD%"=="" (
  set /p FLYWAY_PASSWORD=請輸入 filmax_flyway 的密碼:
)

where flyway >nul 2>nul || (
  echo [!] 找不到 flyway 指令。
  echo     下載 flyway-commandline-*-windows-x64.zip ^(內含 JRE，不必另裝 Java^)
  echo     https://documentation.red-gate.com/fd/command-line-184127404.html
  echo     解壓後把裡面的 flyway.cmd 所在資料夾加進 PATH。
  pause & exit /b 1
)

echo.
echo === 目前狀態 ===
flyway -password="%FLYWAY_PASSWORD%" info
if errorlevel 1 (
  echo.
  echo [X] 連不上或密碼不對。常見原因：
  echo     - 那台 MSSQL 沒開放 1433 給你的來源 IP
  echo     - SQL Server 組態管理員裡沒啟用 TCP/IP 通訊協定
  echo     - filmax_flyway 這個帳號還沒建 ^(先跑 00_create_database.sql^)
  echo     - 密碼打錯
  echo.
  echo     注意：這支是在「你的電腦」上跑的，透過網路連到 DB 主機，
  echo           不需要登入那台伺服器。
  pause & exit /b 1
)

echo.
set /p GO=要套用上面標示 Pending 的遷移嗎? (y/N):
if /i not "%GO%"=="y" (echo 已取消 & pause & exit /b 0)

flyway -password="%FLYWAY_PASSWORD%" migrate
echo.
flyway -password="%FLYWAY_PASSWORD%" info
pause
