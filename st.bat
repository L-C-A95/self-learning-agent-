@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Show tracks: how many tracks, which week each is on.
echo.
"D:/anaconda3/python.exe" tracks.py
echo.
