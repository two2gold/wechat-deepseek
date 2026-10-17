@echo off
rem Stop the tunnel (also tells the -Watch supervisor to exit).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-tunnel.ps1" %*
pause
