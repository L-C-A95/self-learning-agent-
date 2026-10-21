@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "logs\nightly" mkdir "logs\nightly"
rem 计划任务调这个入口。加锁、等 ollama、拉浏览页、跑完推送，都在 nightly.py 里
"D:\anaconda3\python.exe" nightly.py %* >> "logs\nightly\daemon_stdout.log" 2>&1
