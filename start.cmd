@echo off
rem Start the bot in background (detached: closing this window does NOT stop it).
rem ASCII-only on purpose: cmd.exe mis-parses batch files that mix a code page
rem switch (chcp) with multi-byte characters, and it wants CRLF line endings.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-background.ps1" %*
pause
