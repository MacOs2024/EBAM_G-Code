@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title EBAM G-code Studio offline run from existing venv

echo =============================================================
echo EBAM G-code Studio - offline run from existing .venv
 echo =============================================================
echo This mode does NOT use internet and does NOT install packages.
echo It works only if .venv was already created earlier on this PC.
echo.

if not exist ".venv\Scripts\python.exe" goto NO_VENV
".venv\Scripts\python.exe" check_dependencies.py
if errorlevel 1 goto BAD_VENV
".venv\Scripts\python.exe" desktop_launcher.py
pause
exit /b 0

:NO_VENV
echo ERROR: .venv not found.
echo Run run_windows_ROBUST.bat once with internet, or build portable EXE with build_windows_portable_exe.bat.
pause
exit /b 1

:BAD_VENV
echo ERROR: Existing .venv is incomplete.
echo Use internet installer once, or rebuild portable EXE.
pause
exit /b 2
