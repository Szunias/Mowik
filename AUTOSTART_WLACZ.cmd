@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0uruchamianie.ps1" -Mode enable
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Nie udalo sie wlaczyc autostartu.
pause
exit /b %RC%
