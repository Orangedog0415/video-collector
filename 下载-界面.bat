@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -U --no-cache-dir -r requirements.txt -q
where deno >nul 2>nul || winget install -e --id DenoLand.Deno --accept-package-agreements --accept-source-agreements
where ffmpeg >nul 2>nul || winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
python src\gui.py
if errorlevel 1 pause
