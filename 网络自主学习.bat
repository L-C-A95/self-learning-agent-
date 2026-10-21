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
echo   网络自主学习 agent
echo   上网找论文 -> 抓全文 -> 读 -> 学进知识库 -> 出报告
echo.
set /p TOPIC=研究什么（英文原文最好，直接回车则读 研究方向.txt 逐个跑）:
echo.

if "%TOPIC%"=="" (
    %PYTHON% web_agent.py --from-topics --papers 2 --minutes 20
) else (
    %PYTHON% web_agent.py "%TOPIC%" --papers 3 --minutes 20
)

echo.
echo 跑完了。成果报告在当前目录，文件名以「网络成果_」开头。
pause
