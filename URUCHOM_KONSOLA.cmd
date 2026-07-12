@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist "%~dp0.venv\Scripts\python.exe" goto :not_installed
"%~dp0.venv\Scripts\python.exe" "%~dp0mowik.py" --console-log
set "RC=%ERRORLEVEL%"
echo.
echo Program zakonczyl dzialanie z kodem: %RC%
pause
exit /b %RC%

:not_installed
echo Mowik nie jest jeszcze zainstalowany.
echo Najpierw uruchom ZAINSTALUJ.cmd
pause
exit /b 1
