@echo off
chcp 65001 >nul
if "%PYTHON%"=="" set "PYTHON=python"
where /q python 2>nul || set "PYTHON=D:\anaconda3\python.exe"
cd /d "%~dp0"

echo.
echo  ==========================================
echo   注册每天 03:10 自动追踪（跑完推手机）
echo  ==========================================
echo.

rem 以前那个"夜间研究"任务先删掉，两个一起跑会抢 ollama
schtasks /delete /tn "DeepDiveNightlyResearch" /f >nul 2>&1

schtasks /create /tn "AIAutoTrack" /xml "autotrack_task.xml" /f
if errorlevel 1 goto fail

rem 登录后把手机看的网页挂上，白天点链接也能打开
schtasks /create /tn "AIAutoTrackWeb" /tr "\"%PYTHON%\" \"%~dp0serve.py\"" /sc onlogon /f >nul 2>&1
if errorlevel 1 echo   [warn] 浏览页没注册上，白天可以双击「开手机网页.bat」手动开

echo.
echo   已注册：
echo     每天 03:10   AIAutoTrack     自动追线 + 推送到手机
echo     每次登录     AIAutoTrackWeb   手机浏览页 127.0.0.1:8765
echo.
echo   查看状态：schtasks /query /tn "AIAutoTrack" /v /fo list
echo   取消：    双击「取消夜间任务.bat」
echo.
echo   两件事必须自己确认，否则半夜会静默跳过：
echo     1. 那个时间电脑得开着。睡眠能被唤醒，关机不行。
echo     2. 笔记本合盖默认会睡眠 —— 电源设置里把「合上盖子时」
echo        改成「不采取任何操作」，并允许「唤醒定时器」。
echo.
pause
exit /b 0

:fail
echo.
echo   注册失败。右键这个文件 - 以管理员身份运行再试。
echo.
pause
