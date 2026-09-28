@echo off
chcp 65001 >nul
cd /d "%~dp0"
python src\uploader.py --delete --open-perm -j 4 --watch 300
pause
