@echo off
cd /d "%~dp0"
title ORB Bot - Check Kite data
echo Log in to Zerodha from the dashboard (3-DASHBOARD.bat) first - Kite data needs today's login.
echo.
".venv\Scripts\python.exe" data_kite.py check
echo.
pause
