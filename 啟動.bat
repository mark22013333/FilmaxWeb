@echo off
chcp 65001 >nul
cd /d "%~dp0"
title FilmaxWeb

if not exist ".env" (
  echo [!] 找不到 .env，正在從 .env.example 複製一份...
  copy ".env.example" ".env" >nul
  echo [!] 請先用記事本打開 .env 填入 FTP 連線資訊與 TMDB_API_KEY，存檔後再執行一次本檔案。
  pause
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 建立 Python 虛擬環境...
  python -m venv .venv || (echo 找不到 python，請先安裝 Python 3.10 以上並勾選 Add to PATH & pause & exit /b)
)

echo [2/3] 安裝相依套件...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (echo 套件安裝失敗 & pause & exit /b)

where ffmpeg >nul 2>nul || echo [!] 警告: 找不到 ffmpeg，mkv/HEVC 等格式將無法轉碼播放。請安裝 ffmpeg 或在 .env 設定 FFMPEG_PATH。

echo [3/3] 啟動服務...
echo.
start "" http://localhost:8080
".venv\Scripts\python.exe" run.py
pause
