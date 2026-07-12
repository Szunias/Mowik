@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - naprawa instalacji

echo Ten skrypt usunie tylko lokalne srodowisko .venv.
echo Nie usunie konfiguracji, slownika ani pobranego modelu.
echo.
choice /C YN /N /M "Kontynuowac? [Y/N]: "
if errorlevel 2 exit /b 0

if exist "%~dp0.venv" (
  echo Usuwam uszkodzone srodowisko...
  rmdir /S /Q "%~dp0.venv"
)
if exist "%~dp0.venv" (
  echo Nie udalo sie usunac .venv. Zamknij Mowik i sprobuj ponownie.
  pause
  exit /b 1
)
call "%~dp0ZAINSTALUJ.cmd"
exit /b %ERRORLEVEL%
