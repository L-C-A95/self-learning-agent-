@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo   跑一轮追踪，跑完推到手机。大概 10-35 分钟，窗口别关。
echo.
"D:\anaconda3\python.exe" nightly.py --gap 0
echo.
echo   详细过程：logs\nightly\explore_output.log
pause
