#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "已建立 .env，請先填入 FTP / TMDB 設定後再執行一次。"; exit 1; }
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
command -v ffmpeg >/dev/null || echo "[!] 找不到 ffmpeg，mkv/HEVC 將無法轉碼播放"
exec ./.venv/bin/python run.py
