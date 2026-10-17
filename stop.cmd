@echo off
rem Stop the background service (kills the process tree and frees the port).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
pause
