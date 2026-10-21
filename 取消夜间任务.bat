@echo off
chcp 65001 >nul
schtasks /delete /tn "AIAutoTrack" /f >nul 2>&1
schtasks /delete /tn "AIAutoTrackWeb" /f >nul 2>&1
schtasks /delete /tn "DeepDiveNightlyResearch" /f >nul 2>&1
echo.
echo   已取消自动追踪任务。要恢复就双击「注册每日追踪.bat」。
echo.
pause
