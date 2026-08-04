@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - budowanie EXE
set "PYTHONUTF8=1"
if not exist "%~dp0.venv\Scripts\python.exe" goto :not_installed

powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0scripts\build-release.ps1" -Version "2.7.5" -BuildMode UnsignedLocal -PrepareApplicationOnly -PreparedAppManifestPath "%~dp0dist\Mowik-LOCAL-MANIFEST.txt"
if errorlevel 1 goto :fail

echo.
echo Gotowe: %~dp0dist\Mowik\Mowik.exe
echo Manifest plikow: %~dp0dist\Mowik-LOCAL-MANIFEST.txt
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
