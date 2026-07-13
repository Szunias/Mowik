@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - aktualizacja 2.3.0

echo ============================================================
echo   MOWIK - AKTUALIZACJA DO WERSJI 2.3.0
echo ============================================================
echo.
echo Aktualizator sprobuje sam znalezc obecna instalacje.
echo Gdy jej nie znajdzie, poprosi o wskazanie starego folderu Mowik.
echo.

if not exist "%~dp0aktualizuj.ps1" goto :missing_files
if not exist "%~dp0payload\mowik.py" goto :missing_files

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0aktualizuj.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail

echo.
echo Aktualizacja zakonczona. Mowik 2.3.0 zostal uruchomiony.
echo Kliknij prawym przyciskiem ikone przy zegarze i wybierz Panel ustawien.
timeout /t 5 /nobreak >nul
exit /b 0

:missing_files
echo.
echo Brakuje plikow aktualizacji. Najpierw wyodrebnij cala paczke ZIP.
pause
exit /b 3

:fail
echo.
echo Aktualizacja nie powiodla sie. Kod bledu: %RC%
echo Szczegoly sa w pliku aktualizacja.log obok tego skryptu.
pause
exit /b %RC%
