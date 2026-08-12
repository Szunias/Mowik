@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - naprawa instalacji

echo Ten skrypt usunie tylko lokalne srodowisko .venv.
echo Nie usunie konfiguracji, slownika ani pobranego modelu.
echo.
choice /C YN /N /M "Kontynuowac? [Y/N]: "
if errorlevel 2 exit /b 0

echo Odtwarzam srodowisko od zera...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -ForceRebuild
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :repair_failed

echo.
echo Naprawa zakonczona. Uruchamiam Mowik...
call "%~dp0URUCHOM.cmd"
exit /b 0

:repair_failed
echo.
echo Naprawa nie powiodla sie. Kod bledu: %RC%
echo Szczegoly sa w pliku: %~dp0instalacja.log
echo Zamknij Mowik i sprobuj ponownie.
pause
exit /b %RC%
