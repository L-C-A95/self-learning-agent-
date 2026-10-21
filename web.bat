@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Start the phone reading page. Keep window open. Ctrl+C to stop.
echo.
"D:/anaconda3/python.exe" serve.py
echo.
