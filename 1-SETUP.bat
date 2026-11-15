@echo off
cd /d "%~dp0"
title Ayodhya ORB - Setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
