@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Ask the knowledge base. No args = interactive mode.
echo.
"D:/anaconda3/python.exe" ask.py %*
