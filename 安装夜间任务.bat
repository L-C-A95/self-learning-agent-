@echo off
chcp 65001 >nul
rem 注册每天 03:00 自动跑一次研究
rem 任务名用英文，避免计划任务在部分系统上编码出错
schtasks /create /tn "DeepDiveNightlyResearch" /tr "%~dp0run_daemon_night.bat" /sc daily /st 03:00 /f
if errorlevel 1 (
    echo.
    echo 注册失败。常见原因：需要以管理员身份运行这个脚本。
) else (
    echo.
    echo 已注册：每天 03:00 跑一次（预算 30 分钟 / 2 轮 / 每轮最多 2 篇）
    echo 查看：schtasks /query /tn "DeepDiveNightlyResearch"
    echo 取消：双击「取消夜间任务.bat」
)
echo.
pause
