@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Run one tracking round as a SCHEDULED TASK, not as a child of this shell.
rem Why: when Termius / SSH disconnects, every child process dies with it.
rem Windows Task Scheduler owns the process, so it survives disconnection.
schtasks /run /tn "AIAutoTrack" >nul 2>&1
if errorlevel 1 goto notreg
echo Started as scheduled task AIAutoTrack.
echo Safe to close this session or lock the phone now.
echo Progress: logs\nightly\   Result is pushed to your phone when done.
echo Note: it still respects the 3-day gap per track, so it may only look for new leads.
exit /b 0
:notreg
echo [X] Task AIAutoTrack is not registered yet.
echo     On the PC: double-click 注册每日追踪.bat (right-click - Run as administrator),
echo     then run this again from here.
echo     Or, on the PC: double-click 立即追踪一轮.bat
exit /b 1
