@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb - keyframe / segment boundary pre-flight

REM ASCII only in this file: cmd.exe parses a .bat with the console code page,
REM so non-ASCII here gets mis-decoded and the parser runs the garbage.
REM
REM Pre-flight for the two-rung HLS design (spec: J chapter, layer 0).
REM It does NOT encode anything, so it is cheap - a few minutes at most.
REM
REM Three things it answers:
REM   1. COST of getting the keyframe table. Both ffprobe questions have to
REM      demux the whole file. If that takes tens of seconds the table can only
REM      be built at scan time, not at play time. It times both ways and says
REM      which is cheaper.
REM   2. The BOUNDARY TABLE itself - segment count and #EXTINF spread. The
REM      upper rung can only cut on keyframes, so it owns the boundaries and
REM      the lower rung follows them; that is what makes the two rungs
REM      switchable.
REM   3. The real BANDWIDTH value. HLS BANDWIDTH is the PEAK segment bitrate,
REM      not the average. Measured before: a 6s segment of 15.4 MB is about
REM      20.5 Mbps on a file whose average is 10.9. Declaring the average makes
REM      hls.js pick the upper rung on a 12 Mbps link and then stall on every
REM      peak - worse than not offering the rung at all.
REM
REM Same file list as the other probes (quality-probe-list.txt).
REM Results go to segment-boundary-result.txt.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

set LIST=quality-probe-list.txt
set OUT=segment-boundary-result.txt

if not exist "%LIST%" (
  echo Missing %LIST% - nothing to measure.
  pause
  exit /b 1
)

echo Scanning keyframes. No encoding, so this should be minutes not hours.
echo.
"%PY%" -m app.vidprobe --batch "%LIST%" --keyframe-scan --remux-check --no-encode > "%OUT%" 2>&1

echo.
type "%OUT%"
echo.
echo ------------------------------------------------------------
echo Saved to %OUT%
echo ------------------------------------------------------------
pause
