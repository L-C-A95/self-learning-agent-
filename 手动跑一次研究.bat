@echo off
chcp 65001 >nul
if "%PYTHON%"=="" set "PYTHON=python"
where /q python 2>nul || set "PYTHON=D:\anaconda3\python.exe"
rem 手动跑一次，看它现在会研究什么。窗口关掉就停。
cd /d "%~dp0"
curl -s -m 3 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [注意] ollama 好像没在跑，正在尝试启动...
    start "" /b ollama serve
    timeout /t 12 >nul
)
%PYTHON% research_daemon.py --minutes 30 --rounds 2 --papers 2
echo.
echo 跑完了。记录在本目录的 夜间研究_*.md
pause
