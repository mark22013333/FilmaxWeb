@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb quality probe - full run

REM Runs the whole measurement set in one go and writes everything to
REM quality-probe-result.txt so it can be read back / pasted somewhere.
REM
REM Which files get measured is listed in quality-probe-list.txt (UTF-8).
REM Edit that file to add or remove videos - do NOT put the paths in here,
REM cmd.exe mis-decodes non-ASCII in a .bat.
REM
REM Expect roughly 45-75 minutes. --ladder adds an ABR bitrate ladder
REM (x264 and NVENC at the same forced bitrates), which is the only shape
REM that answers "how high does REMOTE_BITRATE_KBPS need to be": a cap only
REM does anything while it binds, so CRF plus different caps measures a flat
REM line. The 4K HDR clip also transcodes slower than real time, and every
REM variant is scored against the source on top of that.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

set LIST=quality-probe-list.txt
set OUT=quality-probe-result.txt

if not exist "%LIST%" (
  echo Missing %LIST% - nothing to measure.
  pause
  exit /b 1
)

echo Measuring... this window will look idle for several minutes. Do not close it.
echo.
"%PY%" -m app.vidprobe --batch "%LIST%" --remux-check --ladder --seconds 30 > "%OUT%" 2>&1

echo.
type "%OUT%"
echo.
echo ------------------------------------------------------------
echo Saved to %OUT%
echo ------------------------------------------------------------
pause
