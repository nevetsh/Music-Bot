@echo off
title Music Bot
cd /d "%~dp0"

set "PYEXE=python"
echo [run.bat] Using Python: %PYEXE%
"%PYEXE%" --version
echo.

echo [run.bat] Starting music bot...
"%PYEXE%" bot.py
pause
