@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "%~dp0.venv\Scripts\pythonw.exe" goto :not_installed
if not exist "%~dp0mowik.py" goto :missing_files
start "" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0mowik.py"
exit /b 0

:not_installed
echo Mowik nie jest jeszcze zainstalowany.
echo Najpierw uruchom ZAINSTALUJ.cmd
pause
exit /b 1

:missing_files
echo Brakuje pliku mowik.py. Rozpakuj ponownie cala paczke.
pause
exit /b 1
