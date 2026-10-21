@echo off
chcp 65001 >nul
if "%PYTHON%"=="" set "PYTHON=python"
where /q python 2>nul || set "PYTHON=D:\anaconda3\python.exe"
cd /d "%~dp0"

rem ---- 检查 ollama 在不在，不在就拉起来 ----
curl -s -m 3 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [daemon] ollama not running, starting...
    start "" /b ollama serve
    timeout /t 12 >nul
)

rem ---- 跑一轮：30 分钟预算 / 2 轮 / 每轮最多 2 篇 ----
echo [daemon] start %date% %time% >> 夜间运行日志.txt
%PYTHON% research_daemon.py --minutes 30 --rounds 2 --papers 2 >> 夜间运行日志.txt 2>&1
echo [daemon] end   %date% %time% >> 夜间运行日志.txt
