@echo off
rem Run in the foreground (Ctrl+C to stop) so you can watch the logs live.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
pause
