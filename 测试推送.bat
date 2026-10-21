@echo off
chcp 65001 >nul
cd /d "%~dp0"
"D:\anaconda3\python.exe" notify.py --test
echo.
pause
