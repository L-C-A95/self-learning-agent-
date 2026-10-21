@echo off
chcp 65001 >nul
echo.
echo   给手机访问放行两个端口（要管理员权限）
echo     8765   手机上读报告
echo     11434  Chatbox 连 ollama
echo.
netsh advfirewall firewall delete rule name="AITrackWeb8765" >nul 2>&1
netsh advfirewall firewall add rule name="AITrackWeb8765" dir=in action=allow protocol=TCP localport=8765
netsh advfirewall firewall delete rule name="Ollama11434" >nul 2>&1
netsh advfirewall firewall add rule name="Ollama11434" dir=in action=allow protocol=TCP localport=11434
echo.
echo   做完后用手机浏览器试：http://你的TailscaleIP:8765
echo   （查本机 IP：tailscale ip -4）
echo.
pause
