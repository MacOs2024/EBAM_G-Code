@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EBAM G-code Studio install from offline wheelhouse

echo Installing dependencies from local .\wheelhouse only.
echo Internet is NOT used.
echo.
if not exist "wheelhouse" goto NO_WHEELHOUSE
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto FAIL
".venv\Scripts\python.exe" -m pip install --no-index --find-links=wheelhouse -r requirements.txt
if errorlevel 1 goto FAIL
".venv\Scripts\python.exe" check_dependencies.py
if errorlevel 1 goto FAIL
".venv\Scripts\python.exe" desktop_launcher.py
pause
exit /b 0

:NO_WHEELHOUSE
echo ERROR: wheelhouse folder not found. Create it on an internet PC using download_wheelhouse_windows.bat.
pause
exit /b 2
:FAIL
echo ERROR: Offline install failed.
pause
exit /b 1
