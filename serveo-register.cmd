@echo off
rem ===================================================================
rem  Serveo registration helper.
rem  Double-click (or run:  serveo-register.cmd mysubdomain).
rem  Serveo will print a link in THIS window - open it, log in, and
rem  register the SSH key it shows. Then re-run this file: it should
rem  print your reserved address  https://<mysubdomain>.serveo.net
rem  Press Ctrl+C to stop the tunnel afterwards.
rem ===================================================================
setlocal
set SUB=%~1
if "%SUB%"=="" set SUB=wxdeepseekai
set KEY=%USERPROFILE%\.ssh\dsh_localhost_run

echo.
echo   Requesting reserved subdomain: %SUB%
echo   Using SSH key: %KEY%
echo.
echo   If Serveo prints "you first need to generate a key / register your key",
echo   open the link it shows, log in at console.serveo.net and add this public key:
echo.
if exist "%KEY%.pub" type "%KEY%.pub"
echo.
echo   ------------------------------------------------------------------
echo.

ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 ^
    -i "%KEY%" -o IdentitiesOnly=yes ^
    -R %SUB%:80:127.0.0.1:8000 serveo.net

echo.
echo   Tunnel ended (or failed). Read the messages above.
pause
