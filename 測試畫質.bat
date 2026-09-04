@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb Video Quality Probe

REM Comments here are ASCII on purpose: cmd.exe parses a .bat with the console
REM code page, so Chinese in a UTF-8 batch file gets mis-decoded and the parser
REM tries to execute the garbage as commands. All Chinese lives in the Python side.
REM
REM Usage:
REM   double click              probe encoders only (~30s)
REM   drag a video file onto it probe + measure that file (~2-5 min)
REM
REM Options: --cap 8000  --crf 21  --seconds 60   (see: python -m app.vidprobe -h)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

"%PY%" -m app.vidprobe %*
echo.
pause
