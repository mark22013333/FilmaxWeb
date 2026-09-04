@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title FilmaxWeb - install ffmpeg 8.1 (BtbN win64 gpl)

REM ---------------------------------------------------------------------------
REM Everything in this file is ASCII on purpose. cmd.exe parses a .bat with the
REM console code page, so non-ASCII comments get mis-decoded and the parser
REM tries to run the garbage as commands. All Chinese lives in the Python side.
REM ---------------------------------------------------------------------------
REM What this does:
REM   1. downloads BtbN's ffmpeg n8.1 win64 gpl build (about 160 MB), or reuses
REM      the copy already in %TEMP% from an earlier run
REM
REM   WHY 8.1 AND NOT 9.0: BtbN pins a different nv-codec-headers version per
REM   ffmpeg branch. 9.0 and master build against Video Codec SDK 13.1, which
REM   demands NVIDIA driver 610 or newer; 8.0/8.1 build against SDK 13.0, which
REM   needs 570 or newer. A GTX 1070 is Pascal, and R580 is the LAST driver
REM   branch NVIDIA ships for Maxwell/Pascal/Volta - so 610 will never exist for
REM   this card and 9.0's NVENC can never load here ("Driver does not support
REM   the required nvenc API version. Required: 13.1 Found: 13.0"). 8.1 still has
REM   libvmaf, libsvtav1, p1..p7, -tune hq and -multipass, so nothing is lost.
REM   2. unpacks it into FFDIR, falling back to FFDIR_ALT if FFDIR cannot be
REM      created (C:\ needs an elevated prompt; the user profile does not)
REM   3. prints the version and the two lines to paste into .env
REM
REM It does NOT touch PATH and does NOT delete anything of yours. Re-running it
REM overwrites the files under the install folder with the newer build.
REM
REM Needs only curl.exe and tar.exe, both shipped with Windows 10/11.
REM ---------------------------------------------------------------------------

set "FFDIR=C:\ffmpeg"
set "FFDIR_ALT=%USERPROFILE%\ffmpeg"
set "ASSET=ffmpeg-n8.1-latest-win64-gpl-8.1.zip"
set "URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/%ASSET%"
set "ZIP=%TEMP%\filmax-%ASSET%"
set "UNPACK=%TEMP%\filmax-ffmpeg-unpack"
set "MINSIZE=100000000"

echo ============================================================
echo  install ffmpeg
echo  source: BtbN FFmpeg-Builds, release branch n8.1, win64 gpl
echo  (8.1, not 9.0 - 9.0 needs NVIDIA driver 610, which Pascal never gets)
echo ============================================================
echo.

where curl.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] curl.exe not found. Windows 10 1803+ ships it.
  echo         Download %URL% manually, unpack it, and copy the bin folder in.
  goto :fail
)
where tar.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] tar.exe not found. Windows 10 1803+ ships it.
  echo         Unpack %ASSET% with 7-Zip instead and copy bin in.
  goto :fail
)

echo [1/5] getting %ASSET%
set "HAVEZIP="
if exist "%ZIP%" (
  for %%F in ("%ZIP%") do if %%~zF GEQ %MINSIZE% set "HAVEZIP=1"
)
if defined HAVEZIP (
  for %%F in ("%ZIP%") do echo       reusing the copy already in TEMP, %%~zF bytes
) else (
  echo       %URL%
  curl.exe -L --fail --retry 3 --retry-delay 3 -o "%ZIP%" "%URL%"
  if errorlevel 1 (
    echo [ERROR] download failed.
    goto :fail
  )
  for %%F in ("%ZIP%") do echo       got %%~zF bytes
)
echo.

echo [2/5] unpacking
if exist "%TEMP%\filmax-ffmpeg-unpack\" rd /s /q "%TEMP%\filmax-ffmpeg-unpack"
mkdir "%UNPACK%" 2>nul
tar.exe -xf "%ZIP%" -C "%UNPACK%"
if errorlevel 1 (
  echo [ERROR] tar could not unpack the archive. It may be a truncated download -
  echo         delete "%ZIP%" and run this again.
  goto :fail
)

set "TOP="
for /d %%D in ("%UNPACK%\*") do set "TOP=%%~fD"
if not defined TOP (
  echo [ERROR] archive layout unexpected: no folder inside.
  goto :fail
)
if not exist "%TOP%\bin\ffmpeg.exe" (
  echo [ERROR] no bin\ffmpeg.exe inside %TOP%
  goto :fail
)
echo       %TOP%
echo.

echo [3/5] choosing where to install
REM C:\ usually refuses a non-elevated mkdir even for an Administrator account:
REM double clicking a .bat gives you an UNELEVATED prompt, and the ACL on the
REM drive root only allows creation from an elevated one. So try, then fall back
REM to the user profile, which always works.
set "TARGET="
mkdir "%FFDIR%" 2>nul
if exist "%FFDIR%\" (
  set "TARGET=%FFDIR%"
) else (
  echo       cannot create %FFDIR% - that needs "Run as administrator".
  echo       falling back to %FFDIR_ALT%
  mkdir "%FFDIR_ALT%" 2>nul
  if exist "%FFDIR_ALT%\" set "TARGET=%FFDIR_ALT%"
)
if not defined TARGET (
  echo [ERROR] could not create either folder. Edit FFDIR at the top of this
  echo         file and point it somewhere you can write.
  goto :fail
)
echo       installing into !TARGET!
echo.

echo [4/5] copying
xcopy "%TOP%\*" "!TARGET!\" /E /I /Y /Q >nul
if errorlevel 1 (
  echo [ERROR] copy failed.
  goto :fail
)
if not exist "!TARGET!\bin\ffmpeg.exe" (
  echo [ERROR] !TARGET!\bin\ffmpeg.exe missing after copy.
  goto :fail
)
echo       done
echo.

echo [5/5] checking the build
echo ------------------------------------------------------------
"!TARGET!\bin\ffmpeg.exe" -hide_banner -version 2>&1 | findstr /i /c:"ffmpeg version"
echo.
echo   filters we need:
"!TARGET!\bin\ffmpeg.exe" -hide_banner -filters 2>nul | findstr /r /c:" libvmaf " /c:" ssim " /c:" zscale " /c:" libplacebo "
echo.
echo   encoders we care about:
"!TARGET!\bin\ffmpeg.exe" -hide_banner -encoders 2>nul | findstr /r /c:" libx264 " /c:" libx265 " /c:" libsvtav1 " /c:" h264_nvenc " /c:" hevc_nvenc "
echo.
echo   nvenc preset constants (p1..p7 means this is 5.0+):
"!TARGET!\bin\ffmpeg.exe" -hide_banner -h encoder=h264_nvenc 2>&1 | findstr /r /c:"^     p[1-7] " /c:"^     hq " /c:"^     fullres "
echo.
echo   nvenc smoke test (encodes 10 frames for real):
"!TARGET!\bin\ffmpeg.exe" -hide_banner -v error -nostats -f lavfi -i testsrc2=size=640x360:rate=30 -frames:v 10 -pix_fmt yuv420p -c:v h264_nvenc -f null - 2>&1
if errorlevel 1 (
  echo       h264_nvenc FAILED - see the message above.
) else (
  echo       h264_nvenc OK
)
echo ------------------------------------------------------------
echo.

echo PASTE THESE TWO LINES INTO .env  (replace the existing ones -
echo if they already say exactly this, nothing to change)
echo ------------------------------------------------------------
echo FFMPEG_PATH=!TARGET!\bin\ffmpeg.exe
echo FFPROBE_PATH=!TARGET!\bin\ffprobe.exe
echo ------------------------------------------------------------
echo   Do NOT wrap them in double quotes: python-dotenv processes escapes
echo   inside double-quoted values, which eats the backslashes.
echo.

echo cleaning up temp files
del /q "%ZIP%" 2>nul
if exist "%TEMP%\filmax-ffmpeg-unpack\" rd /s /q "%TEMP%\filmax-ffmpeg-unpack"
echo.
echo OK. Next: edit .env, then run the quality-probe .bat with NO arguments
echo (about 30 seconds) and send back what it prints.
echo.
pause
exit /b 0

:fail
echo.
echo Nothing was installed. The downloaded archive is kept at
echo   %ZIP%
echo so a re-run will not download it again.
echo.
pause
exit /b 1
