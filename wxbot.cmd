@echo off
rem ===================================================================
rem  Double-click to RUN the bot locally (service + public tunnel).
rem  Close this window (or press Ctrl+C) to STOP everything.
rem  ASCII-only + CRLF on purpose: cmd.exe mis-parses batch files that
rem  mix a code page switch with multi-byte characters.
rem ===================================================================
title WeChat x DeepSeek bot (close this window to stop)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wxbot.ps1" %*
echo.
pause
