@echo off
cd /d "%~dp0"
title Ayodhya ORB - Pushpaka (Kite data check)
echo Log in to Zerodha from the dashboard (3-DASHBOARD.bat) first - Kite data needs today's login.
echo.
".venv\Scripts\python.exe" data_kite.py check
echo.
pause
