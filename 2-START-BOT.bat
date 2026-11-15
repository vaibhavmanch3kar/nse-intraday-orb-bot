@echo off
cd /d "%~dp0"
title Ayodhya ORB - Abhyas (paper trading)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_bot.ps1"
echo.
pause
