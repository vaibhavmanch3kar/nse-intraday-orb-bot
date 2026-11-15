@echo off
cd /d "%~dp0"
title ORB Bot - Dashboard
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard.ps1"
echo.
pause
