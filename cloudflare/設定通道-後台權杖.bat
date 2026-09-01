@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo   Cloudflare Tunnel 安裝   使用後台產生的權杖
echo ============================================================
echo.
echo 這條路不需要瀏覽器授權，通道設定都在 Cloudflare 後台做。
echo.
echo 開始之前，請先在後台完成這些步驟：
echo.
echo   1. 打開 https://one.dash.cloudflare.com
echo   2. 左邊選 Networking，再選 Tunnels
echo   3. 按 Create a tunnel，選 Cloudflared
echo   4. 通道名稱打 filmax，按 Create tunnel
echo   5. 作業系統選 Windows
echo   6. 畫面上會出現一整行安裝指令，裡面有一長串權杖
echo      把那一長串權杖複製起來
echo         指令長這樣：cloudflared.exe service install eyJhIjoi....
echo         你只要複製 install 後面那一段
echo.
echo ------------------------------------------------------------
echo.

REM ---------- cloudflared ----------
where cloudflared >nul 2>&1
if errorlevel 1 (
  echo [1/3] 安裝 cloudflared...
  winget install -e --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements
  where cloudflared >nul 2>&1
  if errorlevel 1 (
    echo   [x] 裝完還是找不到 cloudflared。
    echo       關掉這個視窗，重開一個命令提示字元再跑一次。
    goto FAIL
  )
) else (
  echo [1/3] cloudflared 已安裝
)

REM ---------- 管理員權限 ----------
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo [x] 安裝服務需要系統管理員權限。
  echo     請在這個 .bat 上按右鍵，選「以系統管理員身分執行」。
  goto FAIL
)

REM ---------- 權杖 ----------
echo.
echo [2/3] 貼上權杖
echo.
set "TOKEN="
set /p "TOKEN=權杖: "
if not defined TOKEN (
  echo   [x] 沒有輸入權杖
  goto FAIL
)

REM ---------- 安裝服務 ----------
echo.
echo [3/3] 安裝成 Windows 服務...
sc query cloudflared >nul 2>&1
if not errorlevel 1 (
  echo       先移除舊的服務
  cloudflared service uninstall >nul 2>&1
  timeout /t 2 /nobreak >nul
)
cloudflared service install %TOKEN%
if errorlevel 1 (
  echo.
  echo   [x] 安裝失敗。權杖可能複製不完整。
  echo       它是一長串英數字，開頭通常是 eyJ，中間沒有空格。
  goto FAIL
)
set "TOKEN="

timeout /t 4 /nobreak >nul
echo.
sc query cloudflared | findstr STATE

echo.
echo ============================================================
echo   連線器已安裝
echo ============================================================
echo.
echo 回到 Cloudflare 後台，那個頁面應該已經顯示連線成功。按 Continue。
echo.
echo 接著設定要對外的網址：
echo.
echo   1. 在通道頁面選 Routes 分頁
echo   2. 按 Add route，選 Published application
echo   3. Subdomain 填  video
echo   4. Domain 下拉選  longhopick.com
echo   5. Service Type 選 HTTP
echo   6. URL 填  127.0.0.1:8080
echo   7. 按 Save
echo.
echo 最後改 .env 並重啟 FilmaxWeb：
echo.
echo      HOST=127.0.0.1
echo      PORT=8080
echo      TRUST_PROXY=true
echo      COOKIE_SECURE=true
echo      AUTH_ENABLED=true
echo      AUTH_PASSWORD=夠長的隨機字串
echo      VIEWER_PASSWORD=分享給別人看片用的
echo.
echo   HOST 一定要是 127.0.0.1。留 0.0.0.0 的話，區網裡的人
echo   可以繞過 Cloudflare 直連，那條路沒有 HTTPS 也沒有防護。
echo.
echo 還有一件事，登入紀錄才會有城市與時區：
echo   後台 Rules 　-　Managed Transforms　-　Add visitor location headers
echo.
echo 都好了之後打開： https://video.longhopick.com
echo.
pause
exit /b 0

:FAIL
echo.
echo ------------------------------------------------------------
echo   沒有完成。修正上面的問題後再跑一次。
echo ------------------------------------------------------------
pause
exit /b 1
