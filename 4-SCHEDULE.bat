@echo off
cd /d "%~dp0"
title Ayodhya ORB - Schedule
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_task.ps1"
echo.
pause
