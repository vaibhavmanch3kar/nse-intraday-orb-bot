@echo off
cd /d "%~dp0"
title Ayodhya ORB - stop the bot
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_bot.ps1"
echo.
pause
