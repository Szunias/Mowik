@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mowik - diagnostyka
set "PYTHONUTF8=1"

echo === SYSTEM ===
ver
echo Folder programu: %CD%
echo.

if not exist "%~dp0.venv\Scripts\python.exe" goto :not_installed

echo === PYTHON ===
"%~dp0.venv\Scripts\python.exe" --version
"%~dp0.venv\Scripts\python.exe" -c "import struct,sys; print('Architektura:', struct.calcsize('P')*8, 'bit'); print('Interpreter:', sys.executable)"
echo.

echo === BIBLIOTEKI ===
"%~dp0.venv\Scripts\python.exe" -c "import faster_whisper,ctranslate2,numpy,sounddevice,pynput,pystray,pyperclip,PIL; print('Import bibliotek: OK'); print('CTranslate2:', ctranslate2.__version__); print('NumPy:', numpy.__version__); print('sounddevice:', sounddevice.__version__)"
echo.

echo === MIKROFONY ===
"%~dp0.venv\Scripts\python.exe" "%~dp0mowik.py" --list-devices
echo.

echo === KONFIGURACJA ===
"%~dp0.venv\Scripts\python.exe" "%~dp0mowik.py" --create-config
echo.

echo Log aplikacji: %LOCALAPPDATA%\Mowik\mowik.log
if exist "%LOCALAPPDATA%\Mowik\mowik.log" start "" notepad.exe "%LOCALAPPDATA%\Mowik\mowik.log"
pause
exit /b 0

:not_installed
echo Brak poprawnego folderu .venv.
echo Uruchom ZAINSTALUJ.cmd albo NAPRAW_INSTALACJE.cmd
pause
exit /b 1
