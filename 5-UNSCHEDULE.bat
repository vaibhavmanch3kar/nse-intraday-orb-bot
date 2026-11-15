@echo off
cd /d "%~dp0"
title Ayodhya ORB - Remove schedule
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_task.ps1" -Unregister
echo.
pause
