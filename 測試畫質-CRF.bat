@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb quality probe - CRF / cq ladder

REM ASCII only in this file: cmd.exe parses a .bat with the console code page,
REM so non-ASCII here gets mis-decoded and the parser runs the garbage.
REM
REM This is the follow-up run to the full one. It answers a DIFFERENT question:
REM
REM   the full-run .bat -> "how high does the CAP need to be"
REM                        (bitrate ladder: bitrate is pinned, quality varies)
REM   this file         -> "what should TRANSCODE_CRF and NVENC_CQ be"
REM                        (quality ladder: no cap, the knob picks the bitrate)
REM
REM v4 showed CRF 21 spends only 1093 kbps / VMAF 92.2 on the 4K source while
REM the same source pinned at 2800k reaches 94.6 - so CRF 21 is too conservative.
REM It did not show what the value should be. That is what this measures.
REM
REM Same list of files (quality-probe-list.txt), same per-file sample lengths.
REM Expect roughly 25-40 minutes. Results go to quality-probe-crf-result.txt so
REM the earlier quality-probe-result.txt is not overwritten.
REM
REM FIRST THING TO CHECK in the output: the "alignment self-check" line must be
REM about 100. If it is not, every number below it is comparing the wrong frames
REM and the whole run is void.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

set LIST=quality-probe-list.txt
set OUT=quality-probe-crf-result.txt

if not exist "%LIST%" (
  echo Missing %LIST% - nothing to measure.
  pause
  exit /b 1
)

echo Measuring the CRF / cq ladder. This window will look idle for a long time.
echo Do not close it.
echo.
"%PY%" -m app.vidprobe --batch "%LIST%" --crf-ladder --seconds 30 > "%OUT%" 2>&1

echo.
type "%OUT%"
echo.
echo ------------------------------------------------------------
echo Saved to %OUT%
echo ------------------------------------------------------------
pause
