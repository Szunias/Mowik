@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - budowanie EXE
set "PYTHONUTF8=1"
if not exist "%~dp0.venv\Scripts\python.exe" goto :not_installed

"%~dp0.venv\Scripts\python.exe" -m pip install "PyInstaller==6.21.0"
if errorlevel 1 goto :fail
"%~dp0.venv\Scripts\python.exe" -m pip install --prefer-binary -r "%~dp0requirements-gpu.txt"
if errorlevel 1 goto :fail

"%~dp0.venv\Scripts\python.exe" "%~dp0scripts\generate-icon.py"
if errorlevel 1 goto :fail

"%~dp0.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean "%~dp0packaging\Mowik.spec"
if errorlevel 1 goto :fail

echo.
echo Gotowe: %~dp0dist\Mowik\Mowik.exe
echo Model nie jest dolaczany do EXE.
pause
exit /b 0

:not_installed
echo Najpierw uruchom ZAINSTALUJ.cmd
pause
exit /b 1

:fail
echo Budowanie nie powiodlo sie.
pause
exit /b 1
