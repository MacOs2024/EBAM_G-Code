@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Build EBAM G-code Studio portable Windows EXE

echo =============================================================
echo EBAM G-code Studio - build portable offline Windows EXE
echo =============================================================
echo This build step needs internet once, only on the build PC.
echo After successful build, copy dist\EBAM_Gcode_Studio to any
 echo Windows PC and run EBAM_Gcode_Studio.exe without internet/Python.
echo =============================================================
echo.

set "PYEXE="
call :TRY_EXE "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "%LocalAppData%\Programs\Python\Python313\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files\Python312\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files\Python313\python.exe"
if defined PYEXE goto FOUND
for /f "delims=" %%P in ('where python 2^>nul') do (
    call :TRY_EXE "%%P"
    if defined PYEXE goto FOUND
)
goto NO_PYTHON

:FOUND
echo Selected Python: !PYEXE!
!PYEXE! --version
if not exist ".venv_build\Scripts\python.exe" (
    !PYEXE! -m venv .venv_build
    if errorlevel 1 goto FAIL
)
".venv_build\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto FAIL
".venv_build\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto FAIL
".venv_build\Scripts\python.exe" -m PyInstaller --clean --noconfirm EBAM_Gcode_Studio.spec
if errorlevel 1 goto FAIL

echo.
echo =============================================================
echo BUILD OK.
echo Portable folder:
echo   %CD%\dist\EBAM_Gcode_Studio
echo Run:
echo   dist\EBAM_Gcode_Studio\EBAM_Gcode_Studio.exe
echo You can copy the whole EBAM_Gcode_Studio folder to another Windows PC.
echo =============================================================
pause
exit /b 0

:TRY_EXE
set "CAND=%~1"
if not exist "%CAND%" exit /b 1
"%CAND%" -c "import sys; sys.exit(0 if (sys.version_info[0]==3 and 10 <= sys.version_info[1] <= 13) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%CAND%"
exit /b 0

:NO_PYTHON
echo ERROR: Python 3.10-3.13 not found. Install Python 3.12.x and try again.
pause
exit /b 2

:FAIL
echo ERROR: Build failed. Check messages above.
pause
exit /b 1
