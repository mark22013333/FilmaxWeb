@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   FilmaxWeb - 建立 git 版控並推送到 GitHub
echo ============================================================
echo.

where git >nul 2>&1
if errorlevel 1 (
  echo [x] 找不到 git。先安裝：winget install Git.Git
  echo     裝完重開一個命令提示字元再跑這個檔案。
  pause & exit /b 1
)

REM ---------- 1. 初始化 ----------
if not exist ".git" (
  echo [1/6] 初始化 git 儲存庫...
  git init -q
  git symbolic-ref HEAD refs/heads/main
) else (
  echo [1/6] 已經有 .git，沿用現有儲存庫
)

REM ---------- 2. 身分 ----------
git config user.name  >nul 2>&1 || git config user.name "mark22013333"
git config user.email >nul 2>&1 || git config user.email "mark22013333@users.noreply.github.com"

REM ---------- 3. 安全檢查：機密檔案絕對不能進版控 ----------
echo [2/6] 檢查機密檔案有沒有被排除...
if not exist ".gitignore" (
  echo [x] 找不到 .gitignore，中止。沒有它 .env 會被推上去。
  pause & exit /b 1
)
set "LEAK="
for %%F in (.env data\library.db data\secret.key) do (
  if exist "%%F" (
    git check-ignore -q "%%F"
    if errorlevel 1 (
      echo     [x] %%F 沒有被忽略
      set "LEAK=1"
    ) else (
      echo     [v] %%F 已排除
    )
  )
)
if defined LEAK (
  echo.
  echo [x] 有機密檔案沒被排除，已中止推送。
  echo     請確認 .gitignore 內容正確後再試。
  pause & exit /b 1
)

REM ---------- 4. 加入與提交 ----------
echo [3/6] 加入檔案...
git add -A

echo [4/6] 這次會提交的檔案：
git status --short
echo.

REM 再確認一次暫存區裡沒有 .env
git diff --cached --name-only | findstr /X /C:".env" >nul
if not errorlevel 1 (
  echo [x] .env 竟然出現在暫存區，已中止。
  git reset -q
  pause & exit /b 1
)

git diff --cached --quiet
if errorlevel 1 (
  git commit -q -m "更新 FilmaxWeb" || (echo [x] 提交失敗 & pause & exit /b 1)
  echo     已提交
) else (
  echo     沒有變更需要提交
)

REM ---------- 5. 遠端 ----------
echo [5/6] 設定遠端...
git remote get-url origin >nul 2>&1
if errorlevel 1 (
  git remote add origin https://github.com/mark22013333/FilmaxWeb.git
) else (
  git remote set-url origin https://github.com/mark22013333/FilmaxWeb.git
)
git remote -v

REM ---------- 6. 推送 ----------
echo.
echo [6/6] 推送到 GitHub...
echo     第一次會跳出視窗要你登入 GitHub，照著做即可。
echo.
git push -u origin main
if errorlevel 1 (
  echo.
  echo [x] 推送失敗。常見原因：
  echo     - 儲存庫還沒建立：先到 github.com/new 建立 FilmaxWeb（不要勾 Add a README）
  echo     - 遠端已經有內容：先跑 git pull --rebase origin main 再推
  echo     - 登入沒過：跑 git credential-manager github login
  pause & exit /b 1
)

echo.
echo ============================================================
echo   完成！ https://github.com/mark22013333/FilmaxWeb
echo ============================================================
echo.
echo 之後要再推，直接再跑這個檔案就好。
pause
