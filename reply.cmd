@echo off
rem ===================================================================
rem  Manual reply console - reply to users as the official account.
rem  It lists people who recently messaged you, then you type and send.
rem  Safe to run while wxbot.cmd is running (read-only on the session DB).
rem
rem  Commands inside:  /to N   /users   /followers   /all   /full   /quit
rem  Examples:
rem     reply.cmd                         list recent senders and reply
rem     reply.cmd --openid on6wW3...      reply to a fixed person
rem     reply.cmd --all                   broadcast to all followers
rem ===================================================================
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" "tools\reply.py" %*
echo.
pause
