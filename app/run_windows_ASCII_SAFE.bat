@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title EBAM G-code Studio launcher

echo ===============================================
echo EBAM G-code Studio v4.2.9.18 - Windows SAFE launcher
echo ===============================================
echo.

rem This file is ASCII-only to avoid Russian/Cyrillic BAT encoding problems.
rem It tries Python 3.13, 3.12, 3.11, 3.10 and then plain python.

set "PY="

py -3.13 -c "import sys" >nul 2>nul
if %errorlevel%==0 set "PY=py -3.13"

if not defined PY (
    py -3.12 -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PY=py -3.12"
)

if not defined PY (
    py -3.11 -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PY=py -3.11"
)

if not defined PY (
    py -3.10 -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PY=py -3.10"
)

if not defined PY (
    python -c "import sys" >nul 2>nul
    if %errorlevel%==0 set "PY=python"
)

if not defined PY goto NO_PYTHON

echo Python command selected:
echo %PY%
%PY% --version
if %errorlevel% neq 0 goto PYTHON_BAD

echo.
echo Creating/checking virtual environment...
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if %errorlevel% neq 0 goto VENV_ERROR
)

echo.
echo Activating virtual environment...
call ".venv\Scripts\activate.bat"
if %errorlevel% neq 0 goto ACTIVATE_ERROR

echo.
echo Upgrading pip...
python -m pip install --upgrade pip
if %errorlevel% neq 0 goto PIP_ERROR

echo.
echo Installing requirements...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 goto REQ_ERROR

echo.
echo Starting EBAM G-code Studio...
echo Browser should open automatically.
echo If it does not open, use the local URL shown below, usually http://localhost:8501
echo.
python -m streamlit run app.py --server.address=127.0.0.1 --server.port=8501 --browser.gatherUsageStats=false
pause
exit /b 0

:NO_PYTHON
echo ERROR: Python was not found.
echo Install Python 3.10-3.13 and check the installer option: Add python.exe to PATH
echo Then close this window and run this BAT again.
pause
exit /b 1

:PYTHON_BAD
echo ERROR: Python was found but cannot run correctly.
pause
exit /b 2

:VENV_ERROR
echo ERROR: Could not create .venv.
echo Try moving the folder to a simple path like C:\EBAM_Gcode_Studio and run again.
pause
exit /b 3

:ACTIVATE_ERROR
echo ERROR: Could not activate .venv.
pause
exit /b 4

:PIP_ERROR
echo ERROR: Could not upgrade pip.
echo Check internet connection.
pause
exit /b 5

:REQ_ERROR
echo ERROR: Could not install requirements.
echo Recommended fix: install Python 3.12 or 3.13, then delete .venv and run again.
echo Current Python may be too new for some binary packages.
pause
exit /b 6
