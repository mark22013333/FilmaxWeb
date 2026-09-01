@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "TUNNEL=filmax"
set "HOSTNAME=video.longhopick.com"
set "LOCALPORT=8080"
set "CFDIR=%USERPROFILE%\.cloudflared"

echo ============================================================
echo   Cloudflare Tunnel 設定   %HOSTNAME%
echo ============================================================
echo.
echo 這個腳本可以重複執行，已完成的步驟會自動跳過。
echo.

REM ============================ 1. cloudflared ============================
where cloudflared >nul 2>&1
if errorlevel 1 goto INSTALL_CF
echo [1/6] cloudflared 已安裝
for /f "tokens=*" %%v in ('cloudflared --version 2^>^&1') do echo       %%v
goto STEP2

:INSTALL_CF
echo [1/6] 安裝 cloudflared...
winget install -e --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements
where cloudflared >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [x] 裝完還是找不到 cloudflared。
  echo       請關掉這個視窗，重開一個命令提示字元再跑一次。
  echo       PATH 的變更要新開的視窗才會生效。
  goto FAIL
)

REM ============================ 2. 授權 ============================
:STEP2
echo.
if exist "%CFDIR%\cert.pem" (
  echo [2/6] 已授權過這個 Cloudflare 帳號
  goto STEP3
)

echo [2/6] 需要授權 Cloudflare 帳號
echo.
echo   接下來會印出一個授權網址。
echo   如果瀏覽器沒有自動打開，請把那個網址整段複製到瀏覽器貼上。
echo.
echo   在網頁上要做的事：
echo     1. 登入你的 Cloudflare 帳號
echo     2. 在網域清單選 longhopick.com
echo     3. 按 Authorize
echo.
echo   授權完成後這個視窗會自己往下跑。
echo   -----------------------------------------------------------
echo.
cloudflared tunnel login
echo.
echo   -----------------------------------------------------------
if not exist "%CFDIR%\cert.pem" (
  echo.
  echo   [x] 沒有拿到授權憑證 %CFDIR%\cert.pem
  echo.
  echo       常見原因：
  echo       - 網頁上沒有選到 longhopick.com 就按了 Authorize
  echo       - 授權網址過期了，重跑這個腳本會產生新的
  echo       - 登入的 Cloudflare 帳號底下沒有這個網域
  echo.
  echo       想單獨重試這一步：cloudflared tunnel login
  goto FAIL
)
echo       授權完成

REM ============================ 3. 建立通道 ============================
:STEP3
echo.
cloudflared tunnel list 2>nul | findstr /C:"%TUNNEL%" >nul
if errorlevel 1 (
  echo [3/6] 建立通道 %TUNNEL%...
  cloudflared tunnel create %TUNNEL%
  if errorlevel 1 (
    echo   [x] 建立通道失敗
    goto FAIL
  )
) else (
  echo [3/6] 通道 %TUNNEL% 已存在
)

set "TID="
for /f "tokens=1" %%i in ('cloudflared tunnel list 2^>nul ^| findstr /C:"%TUNNEL%"') do set "TID=%%i"
if not defined TID (
  echo   [x] 取不到通道 ID。手動看一下：cloudflared tunnel list
  goto FAIL
)
echo       通道 ID: %TID%

if not exist "%CFDIR%\%TID%.json" (
  echo   [x] 找不到通道憑證 %CFDIR%\%TID%.json
  echo       這個通道可能是在別台機器建的。
  echo       解法：cloudflared tunnel delete %TUNNEL%  之後重跑本腳本
  goto FAIL
)

REM ============================ 4. 設定檔 ============================
echo.
echo [4/6] 產生設定檔 %CFDIR%\config.yml
> "%CFDIR%\config.yml" echo tunnel: %TID%
>> "%CFDIR%\config.yml" echo credentials-file: %CFDIR%\%TID%.json
>> "%CFDIR%\config.yml" echo.
>> "%CFDIR%\config.yml" echo ingress:
>> "%CFDIR%\config.yml" echo   - hostname: %HOSTNAME%
>> "%CFDIR%\config.yml" echo     service: http://127.0.0.1:%LOCALPORT%
>> "%CFDIR%\config.yml" echo     originRequest:
>> "%CFDIR%\config.yml" echo       connectTimeout: 30s
>> "%CFDIR%\config.yml" echo   - service: http_status:404
echo.
type "%CFDIR%\config.yml"

REM ============================ 5. DNS ============================
echo.
echo [5/6] 把 %HOSTNAME% 指向這個通道...
cloudflared tunnel route dns %TUNNEL% %HOSTNAME%
if errorlevel 1 (
  echo       已經指過的話會顯示錯誤，那是正常的，可以繼續。
)

REM ============================ 6. 服務 ============================
echo.
net session >nul 2>&1
if errorlevel 1 (
  echo [6/6] 略過：安裝服務需要系統管理員權限
  echo.
  echo       前面的步驟都完成了。要裝成開機自動啟動的服務，
  echo       請在這個 .bat 上按右鍵，選「以系統管理員身分執行」再跑一次。
  echo.
  echo       或者先手動跑起來測試： cloudflared tunnel run %TUNNEL%
  goto DONE
)

echo [6/6] 安裝成 Windows 服務...
sc query cloudflared >nul 2>&1
if not errorlevel 1 (
  echo       先移除舊的服務
  cloudflared service uninstall >nul 2>&1
  timeout /t 2 /nobreak >nul
)
cloudflared service install
if errorlevel 1 (
  echo   [x] 安裝服務失敗
  goto FAIL
)
timeout /t 3 /nobreak >nul
sc query cloudflared | findstr STATE

REM ============================ 完成 ============================
:DONE
echo.
echo ============================================================
echo   通道設定完成
echo ============================================================
echo.
echo 還有兩件事要做，做完才會通：
echo.
echo   一、改 .env 然後重啟 FilmaxWeb
echo.
echo        HOST=127.0.0.1
echo        PORT=%LOCALPORT%
echo        TRUST_PROXY=true
echo        COOKIE_SECURE=true
echo        AUTH_ENABLED=true
echo        AUTH_PASSWORD=夠長的隨機字串
echo        VIEWER_PASSWORD=分享給別人看片用的
echo.
echo        HOST 一定要改成 127.0.0.1。留 0.0.0.0 的話，
echo        區網裡的人可以繞過 Cloudflare 直連，那條路沒有 HTTPS。
echo.
echo   二、到 Cloudflare 後台開啟訪客位置標頭
echo        Rules 　-　Managed Transforms　-　Add visitor location headers
echo        不開的話登入紀錄只有國家，沒有城市與時區。
echo.
echo 都做完之後打開： https://%HOSTNAME%
echo.
echo 常用指令：
echo    sc query cloudflared              服務狀態
echo    cloudflared tunnel info %TUNNEL%      通道連線數
echo    cloudflared tunnel run %TUNNEL%       前景執行，看即時錯誤
echo.
pause
exit /b 0

:FAIL
echo.
echo ------------------------------------------------------------
echo   沒有完成。修正上面的問題後再跑一次這個腳本即可，
echo   已經完成的步驟會自動跳過。
echo ------------------------------------------------------------
pause
exit /b 1
