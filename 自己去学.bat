@echo off
chcp 65001 >nul
if "%PYTHON%"=="" set "PYTHON=python"
where /q python 2>nul || set "PYTHON=D:\anaconda3\python.exe"
cd /d "%~dp0"

rem ---- Ollama 没在跑就拉起来 ----
curl -s -m 3 http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [启动] Ollama 未运行，正在拉起...
    start "" /b ollama serve
    timeout /t 12 >nul
)

echo.
echo   自己逛：先接着追之前的话题，再自由挑新方向，最后给你一份「每日发现」
echo   不用你输任何题目。逛 3 轮，大概 10-18 分钟。追踪线追满 8 周会自动收线。
echo.
%PYTHON% explore.py --rounds 3 --papers 2 --minutes 25

echo.
echo 跑完了。看 产出\每日发现\ 里最新的那份；术语攒在 产出\术语表.md。
pause
