@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title EBAM G-code Studio v4.2.9.18 launcher

echo ================================================
echo EBAM G-code Studio v4.2.9.18 - robust Windows launcher
echo ================================================
echo.
echo This launcher avoids the Windows py launcher if it is broken.
echo It prefers real Python 3.12 / 3.13 executables.
echo.

set "PYEXE="
set "PYVER="

rem 1) Try typical per-user Python installation paths first.
call :TRY_EXE "%LocalAppData%\Programs\Python\Python313\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "%LocalAppData%\Programs\Python\Python312\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "%LocalAppData%\Programs\Python\Python311\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "%LocalAppData%\Programs\Python\Python310\python.exe"
if defined PYEXE goto FOUND

rem 2) Try common all-users installation paths.
call :TRY_EXE "C:\Program Files\Python313\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files\Python312\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files\Python311\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files\Python310\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files (x86)\Python313\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files (x86)\Python312\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files (x86)\Python311\python.exe"
if defined PYEXE goto FOUND
call :TRY_EXE "C:\Program Files (x86)\Python310\python.exe"
if defined PYEXE goto FOUND

rem 3) Try python.exe from PATH. This ignores broken Microsoft Store stubs.
for /f "delims=" %%P in ('where python 2^>nul') do (
    call :TRY_EXE "%%P"
    if defined PYEXE goto FOUND
)

rem 4) Try Windows Python launcher, but validate it strictly.
call :TRY_CMD "py -3.13"
if defined PYEXE goto FOUND
call :TRY_CMD "py -3.12"
if defined PYEXE goto FOUND
call :TRY_CMD "py -3.11"
if defined PYEXE goto FOUND
call :TRY_CMD "py -3.10"
if defined PYEXE goto FOUND

goto NO_PYTHON

:FOUND
echo Selected Python:
echo   !PYEXE!
echo Version:
!PYEXE! -c "import sys; print(sys.version)"
if errorlevel 1 goto PYTHON_BAD

echo.
echo Creating/checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    !PYEXE! -m venv .venv
    if errorlevel 1 goto VENV_ERROR
)

echo.
echo Using virtual environment Python:
".venv\Scripts\python.exe" --version
if errorlevel 1 goto VENV_ERROR

echo.
echo Upgrading pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto PIP_ERROR

echo.
echo Installing requirements...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto REQ_ERROR

echo.
echo Checking Python dependencies...
".venv\Scripts\python.exe" check_dependencies.py
if errorlevel 1 goto REQ_ERROR

echo.
echo Starting EBAM G-code Studio...
echo If browser does not open, use the local URL shown below, usually http://localhost:8501
echo.
".venv\Scripts\python.exe" -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --browser.gatherUsageStats=false
pause
exit /b 0

:TRY_EXE
set "CAND=%~1"
if not exist "%CAND%" exit /b 1
"%CAND%" -c "import sys; sys.exit(0 if (sys.version_info[0]==3 and 10 <= sys.version_info[1] <= 13) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%CAND%"
exit /b 0

:TRY_CMD
set "CAND=%~1"
%CAND% -c "import sys; sys.exit(0 if (sys.version_info[0]==3 and 10 <= sys.version_info[1] <= 13) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%CAND%"
exit /b 0

:NO_PYTHON
echo ERROR: Suitable Python 3.10-3.13 was not found.
echo.
echo What to do:
echo 1. Install Python 3.12.x from python.org.
echo 2. During install, enable: Add python.exe to PATH.
echo 3. Close this window, delete .venv if it exists, run this BAT again.
echo.
echo Diagnostics:
echo where python:
where python 2>nul
echo.
echo where py:
where py 2>nul
echo.
echo py -0p:
py -0p 2>nul
pause
exit /b 1

:PYTHON_BAD
echo ERROR: Selected Python cannot run correctly.
pause
exit /b 2

:VENV_ERROR
echo ERROR: Could not create or use .venv.
echo Try moving the folder to a simple path like C:\EBAM_Gcode_Studio and run again.
pause
exit /b 3

:PIP_ERROR
echo ERROR: Could not upgrade pip. Check internet connection.
pause
exit /b 4

:REQ_ERROR
echo ERROR: Could not install requirements.
echo Recommended: use Python 3.12.x, delete .venv, then run again.
pause
exit /b 5
