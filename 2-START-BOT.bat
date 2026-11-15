@echo off
cd /d "%~dp0"
title ORB Bot - Paper trading
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_bot.ps1"
echo.
pause
