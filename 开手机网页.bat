@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo   这个窗口别关，关了手机就打不开了。Ctrl+C 停止。
echo.
"D:\anaconda3\python.exe" serve.py --open
pause
