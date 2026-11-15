@echo off
cd /d "%~dp0"
title ORB Bot - Setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
