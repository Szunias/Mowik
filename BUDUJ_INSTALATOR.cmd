@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - budowanie instalatora Windows
set "PYTHONUTF8=1"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-release.ps1" -Version "2.5.0"
if errorlevel 1 goto :fail

echo.
echo Gotowe. Instalator znajdziesz w folderze:
echo %~dp0release
pause
exit /b 0

:fail
echo.
echo Budowanie instalatora nie powiodlo sie.
pause
exit /b 1
