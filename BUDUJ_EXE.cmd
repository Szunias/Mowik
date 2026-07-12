@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - budowanie EXE
set "PYTHONUTF8=1"
if not exist "%~dp0.venv\Scripts\python.exe" goto :not_installed

"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

"%~dp0.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --onedir ^
  --name Mowik ^
  --collect-all faster_whisper ^
  --collect-all ctranslate2 ^
  --collect-all av ^
  --collect-all sounddevice ^
  --collect-all pystray ^
  --collect-all pyperclip ^
  --collect-all PIL ^
  "%~dp0mowik.py"
if errorlevel 1 goto :fail

copy /Y "%~dp0README.md" "%~dp0dist\Mowik\README.md" >nul
copy /Y "%~dp0config.example.json" "%~dp0dist\Mowik\config.example.json" >nul
copy /Y "%~dp0slownik.example.txt" "%~dp0dist\Mowik\slownik.example.txt" >nul

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
