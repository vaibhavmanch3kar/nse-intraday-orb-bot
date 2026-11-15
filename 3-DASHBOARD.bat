@echo off
cd /d "%~dp0"
title Ayodhya ORB - Ayodhya Darbar (dashboard)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard.ps1"
echo.
pause
