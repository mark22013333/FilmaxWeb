@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title FilmaxWeb - Stop

REM ASCII only in the comments of this file. cmd.exe parses a .bat line by line
REM using the console code page, and `chcp 65001` on line 2 does not retroactively
REM apply to how the parser decoded what follows on some hosts -- a REM line with
REM multi-byte characters gets mis-split and its tail runs as a command. That is
REM exactly the "'...' is not recognized as an internal or external command" noise.
REM The same warning is written at the top of the boot .bat; heed it here too.
REM Text printed with `echo` is fine in Chinese -- that is output, not source the
REM parser has to tokenize.
REM
REM Stop the service. Pairs with the launcher .bat, and also ends the scheduled
REM task that the boot .bat registers.
REM
REM Why match by port instead of `taskkill /im python.exe`:
REM other projects on this box run their own python.exe (ChatG-Bot mcp_server and
REM friends), so killing by image name takes unrelated services down with it.
REM The port is this service's only unique identity.
REM
REM Why /t: the process holding the port is not always run.py itself -- under
REM reload mode run.py is the parent and the real server is a child, so killing
REM one side leaves the other half alive. /t collects children too, so it comes
REM out clean whichever one we catch first.

echo.
echo   === FilmaxWeb 停止服務 ===
echo.

REM Read the port from .env, same rule as the launcher -- hardcoding it means
REM changing the setting later silently stops the wrong thing.
REM findstr /B matches line starts only, so comment lines that mention PORT
REM further along do not get picked up.
set "PORT="
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%a in (`findstr /B /C:"PORT=" ".env"`) do set "PORT=%%b"
)
if not defined PORT set "PORT=8080"
REM Strip trailing whitespace or an end-of-line comment
for /f "tokens=1" %%p in ("!PORT!") do set "PORT=%%p"

echo   [1/3] 停用排程任務（避免被自動拉回來）...
REM Stop the schedule before killing the process; the order matters. If the task
REM has retry-on-failure set, killing first lets it come back minutes later and
REM it looks like the service refuses to stop.
REM /end only ends the current run -- the task still starts at next boot, which
REM is deliberate. /change /disable would let one stop quietly kill boot startup
REM for good, which is not what "stop the service" should mean.
schtasks /query /tn "FilmaxWeb" >nul 2>nul
if !ERRORLEVEL! EQU 0 (
  schtasks /end /tn "FilmaxWeb" >nul 2>nul
  if !ERRORLEVEL! EQU 0 (
    echo         排程任務 FilmaxWeb 已結束本次執行
  ) else (
    echo         排程任務 FilmaxWeb 目前沒在執行
  )
) else (
  echo         找不到排程任務 FilmaxWeb，略過（沒設開機啟動就是正常的）
)

echo   [2/3] 尋找佔用埠 !PORT! 的行程...
set "FOUND="
REM Only the LISTENING row is the service itself; inbound client connections
REM must not count. tokens=5 is the PID column, and the leading colon in the
REM pattern keeps :29880 from also matching something like 129880.
for /f "tokens=5" %%p in ('netstat -ano -p tcp ^| findstr /R /C:":!PORT! .*LISTENING"') do (
  if not "%%p"=="0" (
    set "FOUND=1"
    echo         找到 PID %%p，正在結束...
    taskkill /pid %%p /t /f >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
      echo         PID %%p 已結束
    ) else (
      echo         [!] PID %%p 結束失敗，可能需要用系統管理員身分執行本檔案
    )
  )
)

if not defined FOUND (
  echo         埠 !PORT! 沒有人在監聽，服務應該已經是停止狀態
)

echo   [3/3] 確認結果...
REM A killed process does not release the port instantly -- the OS needs a
REM moment. Without this wait you get "stopped fine" followed by "port already
REM in use" on the next start, which is a maddening thing to debug.
REM No `timeout /t` here: it wants a console and fails under a scheduled task.
ping -n 3 127.0.0.1 >nul 2>nul
netstat -ano -p tcp | findstr /R /C:":!PORT! .*LISTENING" >nul 2>nul
if !ERRORLEVEL! EQU 0 (
  echo.
  echo   [!] 埠 !PORT! 仍被佔用，服務可能沒有完全停止。
  echo       請用系統管理員身分重新執行本檔案，或手動檢查：
  echo       netstat -ano ^| findstr :!PORT!
  echo.
  pause
  exit /b 1
)

echo.
echo   服務已停止（埠 !PORT! 已釋放）
echo.
pause
exit /b 0