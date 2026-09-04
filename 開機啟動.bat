@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title FilmaxWeb (boot)

REM ASCII only in this file: cmd.exe parses a .bat with the console code page,
REM so non-ASCII here gets mis-decoded and the parser runs the garbage.
REM Also: no judgement logic inside a quoted `python -c` -- cmd eats ^ and !
REM before Python ever sees them (see docs box 0 for the two hours that cost).
REM
REM This is the unattended entry point, driven by Task Scheduler at system
REM startup. It differs from the by-hand launcher (the one you double-click) in three ways, and each
REM difference is the point of having a second file rather than a flag:
REM
REM   1. No `pip install`. At boot there is no one to read a failure, and
REM      reinstalling packages on every power-on costs 10-40s for nothing.
REM      Dependencies change when a human changes them -- run the installer .bat then.
REM   2. No browser. Nothing is asking for a window at boot; on a headless
REM      restart `start http://...` either does nothing or steals focus later.
REM   3. Output goes to a log, not a console. A scheduled task has no visible
REM      console, so anything printed to a vanished stdout is lost -- and the
REM      one time it matters is the boot that failed.

set "LOGDIR=data"
set "LOG=%LOGDIR%\boot.log"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

REM Keep the log from growing without bound. 5MB is ~2 months of restarts;
REM one generation back is enough to cover "it broke after last night's boot".
for %%F in ("%LOG%") do if %%~zF GTR 5242880 move /y "%LOG%" "%LOG%.1" >nul 2>nul

echo. >> "%LOG%"
echo ========================================================== >> "%LOG%"
echo [%DATE% %TIME%] boot start >> "%LOG%"

if not exist ".env" (
  echo [%DATE% %TIME%] FATAL: .env missing, refusing to start >> "%LOG%"
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [%DATE% %TIME%] FATAL: .venv missing, run the installer .bat once by hand >> "%LOG%"
  exit /b 1
)

REM The library lives on D:, which is a separate volume from the OS. A service
REM starting at boot can beat the disk to being mounted, and the failure mode is
REM ugly: the scan finds nothing, marks every file vanished, and purge cleans up
REM a library that was only ever missing. So wait for the root to actually exist
REM before handing over to Python. 60 x 2s = 2 minutes, then give up loudly.
REM (Task Scheduler retries after that, so giving up here is not the end.)
set "WAITROOT=D:\1.FTP"
set /a TRIES=0
:waitloop
if exist "%WAITROOT%\" goto rootok
set /a TRIES+=1
if %TRIES% GEQ 60 (
  echo [%DATE% %TIME%] FATAL: %WAITROOT% never appeared after 120s >> "%LOG%"
  exit /b 1
)
REM No `timeout /t` here: it needs a console and errors out under a scheduled
REM task ("input redirection is not supported"). ping the loopback instead.
ping -n 3 127.0.0.1 >nul 2>nul
goto waitloop

:rootok
echo [%DATE% %TIME%] %WAITROOT% present after %TRIES% retries >> "%LOG%"
echo [%DATE% %TIME%] starting run.py >> "%LOG%"

REM 2>&1 so tracebacks land in the log too -- uvicorn writes its startup lines
REM and every error to stderr, which is exactly what you want to read later.
".venv\Scripts\python.exe" run.py >> "%LOG%" 2>&1

REM Only reached when the server exits. Task Scheduler reads this exit code to
REM decide whether to retry, so pass Python's through unchanged.
echo [%DATE% %TIME%] run.py exited with code %ERRORLEVEL% >> "%LOG%"
exit /b %ERRORLEVEL%
