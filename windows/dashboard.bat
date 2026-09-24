@echo off
REM Starts the Accusignals web dashboard with the live scanner running.
REM   windows\dashboard.bat                     -> http://127.0.0.1:8765 (this PC only)
REM   windows\dashboard.bat --host 0.0.0.0      -> also reachable from your phone on the same Wi-Fi (token protected)
REM Restarts automatically if the process ever exits (network drop, crash).
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Run windows\setup.bat first. 1>&2
  exit /b 1
)
set CONFIG=
if exist config.json set CONFIG=--config config.json
:loop
.venv\Scripts\python.exe -m accusignals dashboard --start %CONFIG% %*
echo [%date% %time%] dashboard exited with %ERRORLEVEL%, restarting in 15s... 1>&2
timeout /t 15 /nobreak >nul
goto loop
