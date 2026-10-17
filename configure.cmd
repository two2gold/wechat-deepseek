@echo off
rem One-shot setup: write credentials into .env, restart, then self-check.
rem Example:
rem   configure.cmd -AppId wx123 -AppSecret abc -DeepSeekKey sk-xxx
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure.ps1" %*
pause
