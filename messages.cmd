@echo off
rem ===================================================================
rem  Show incoming user messages with timestamps.
rem  Default: print the latest 20 and keep following new ones live.
rem  Close the window (or Ctrl+C) to stop following.
rem
rem  Only history, no following:
rem     messages.cmd --once --last 50
rem ===================================================================
setlocal
cd /d "%~dp0"
set ARGS=%*
if "%ARGS%"=="" set ARGS=--last 20
".venv\Scripts\python.exe" "tools\tail_messages.py" %ARGS%
echo.
pause
