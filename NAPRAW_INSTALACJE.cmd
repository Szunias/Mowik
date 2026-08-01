@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - naprawa instalacji

echo Ten skrypt usunie tylko lokalne srodowisko .venv.
echo Nie usunie konfiguracji, slownika ani pobranego modelu.
echo.
choice /C YN /N /M "Kontynuowac? [Y/N]: "
if errorlevel 2 exit /b 0

echo Bezpiecznie usuwam uszkodzone srodowisko...
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0install.ps1" -RemovePrivateEnvironmentOnly
if errorlevel 1 goto :remove_failed
call "%~dp0ZAINSTALUJ.cmd"
exit /b %ERRORLEVEL%

:remove_failed
echo Nie udalo sie bezpiecznie usunac .venv. Zamknij Mowik i sprobuj ponownie.
pause
exit /b 1
