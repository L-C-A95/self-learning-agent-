@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Run one round now (ignore 3-day gap), push to phone when done. Keep window open.
echo.
"D:/anaconda3/python.exe" nightly.py --gap 0
echo.
