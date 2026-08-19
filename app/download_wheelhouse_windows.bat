@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Download EBAM G-code Studio offline wheelhouse

echo This downloads all Python packages into .\wheelhouse.
echo Run on a Windows PC WITH internet. Then copy the whole folder
echo including wheelhouse to a Windows PC WITHOUT internet.
echo.

set "PYEXE=python"
%PYEXE% --version
if errorlevel 1 goto NO_PY
if not exist "wheelhouse" mkdir wheelhouse
%PYEXE% -m pip download -r requirements.txt -d wheelhouse
if errorlevel 1 goto FAIL
%PYEXE% -m pip download pyinstaller -d wheelhouse

echo Wheelhouse ready: %CD%\wheelhouse
pause
exit /b 0

:NO_PY
echo ERROR: python not found.
pause
exit /b 2
:FAIL
echo ERROR: wheelhouse download failed.
pause
exit /b 1
