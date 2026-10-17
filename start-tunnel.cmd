@echo off
rem Public HTTPS tunnel with self-healing (-Watch). Keep this window open.
rem One-shot instead (exit right after printing the URL):
rem   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-tunnel.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-tunnel.ps1" -Watch %*
pause
