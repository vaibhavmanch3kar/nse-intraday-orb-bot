@echo off
cd /d "%~dp0"
title ORB Bot - Schedule
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register_task.ps1"
echo.
pause
