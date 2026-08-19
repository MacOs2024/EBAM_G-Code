@echo off
chcp 65001 >nul
echo Проверка Python...
echo.
where py
py -3 --version
echo.
where python
python --version
echo.
echo Если выше написано, что команда не найдена, установите Python и включите Add python.exe to PATH.
pause
