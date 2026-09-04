@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title FilmaxWeb - run every tests\*_test.py

REM ASCII only in this file: cmd.exe parses a .bat with the console code page,
REM so non-ASCII here gets mis-decoded and the parser runs the garbage.
REM
REM Runs every tests\*_test.py the same way you would by hand (they are
REM standalone scripts, not pytest), one after another, and reports which ones
REM came back non-zero. Full output goes to test-result.txt.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
REM Which interpreter: .venv by default, pass "sys" as the first argument to
REM force the system one. This matters - a stale .venv can be missing playwright
REM or pyftpdlib and carry an older starlette, and then scripts fail on the
REM environment rather than on the code. The log records which one actually ran.
set PY=.venv\Scripts\python.exe
if /i "%~1"=="sys" set PY=python
if not exist "%PY%" set PY=python

set OUT=test-result.txt
set FAILED=
set /a TOTAL=0
set /a BAD=0

echo Running tests, full output goes to %OUT%
echo.
break > "%OUT%"
echo ========================================================== >> "%OUT%"
echo # interpreter and package versions >> "%OUT%"
echo ========================================================== >> "%OUT%"
"%PY%" -c "import sys;print(sys.executable);print(sys.version)" >> "%OUT%" 2>&1
for %%P in (fastapi starlette httpx anyio python-dotenv playwright pyftpdlib) do (
  "%PY%" -c "import importlib.metadata as m;print('%%P', m.version('%%P'))" >> "%OUT%" 2>&1 || echo %%P NOT INSTALLED >> "%OUT%"
)
"%PY%" -c "import sys;print('interpreter:', sys.executable)"

REM Pinned-version check. A venv can satisfy requirements.txt loosely and still
REM be wrong: pip only touches a dependency when the installed version breaks a
REM constraint, so a stale transitive package (starlette is the one that bit us)
REM survives an upgrade of the package that pulls it. Comparing the "==" pins
REM against what is installed makes a wrong environment look like a wrong
REM environment instead of a TypeError raised inside a test.
REM
REM The logic lives in tests\pins_check.py and NOT in a python -c one-liner
REM here, because cmd.exe eats two characters such a one-liner needs: the
REM escape character (it swallowed the regex anchors and every negated class)
REM and the delayed-expansion marker (it turned not-equal into plain equal).
REM Rule: never put those two characters inside a quoted python -c in a .bat.
REM (This file does use the delayed-expansion marker on its own variables at
REM the bottom - that is fine, cmd expands them on purpose.)
"%PY%" tests\pins_check.py >> "%OUT%" 2>&1
"%PY%" tests\pins_check.py
echo.

for %%T in (tests\*_test.py) do (
  set /a TOTAL+=1
  echo ---------------------------------------------------------- >> "%OUT%"
  echo # %%T >> "%OUT%"
  echo ---------------------------------------------------------- >> "%OUT%"
  "%PY%" "%%T" >> "%OUT%" 2>&1
  if errorlevel 1 (
    echo   FAIL  %%T
    set /a BAD+=1
    set FAILED=!FAILED! %%T
  ) else (
    echo   ok    %%T
  )
)

echo.
echo ------------------------------------------------------------
if !BAD! GTR 0 (
  echo !BAD! of !TOTAL! scripts exited non-zero:!FAILED!
  echo Open %OUT% and search for FAIL to see which assertions broke.
) else (
  echo All !TOTAL! scripts exited 0.
)
echo Per-script totals are in %OUT% - the last line of each block.
echo ------------------------------------------------------------
pause
