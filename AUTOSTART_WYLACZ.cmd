@echo off
setlocal EnableExtensions
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0autostart.ps1" -Mode disable
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Nie udalo sie wylaczyc autostartu.
pause
exit /b %RC%
