@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - instalacja

echo ============================================================
echo   MOWIK - lokalne dyktowanie push-to-talk dla Windows
echo ============================================================
echo.
echo Instalator uruchamia sie z folderu:
echo %CD%
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail

echo.
echo Instalacja zakonczona. Uruchamiam Mowik...
call "%~dp0URUCHOM.cmd"
exit /b 0

:fail
echo.
echo Instalacja nie powiodla sie. Kod bledu: %RC%
echo Szczegoly sa w pliku: %~dp0instalacja.log
echo Mozesz tez uruchomic DIAGNOSTYKA.cmd
pause
exit /b %RC%
