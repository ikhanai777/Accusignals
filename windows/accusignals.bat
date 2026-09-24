@echo off
REM Wrapper so agents/Task Scheduler can call: windows\accusignals.bat scan --json
setlocal
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe (
  echo Run windows\setup.bat first. 1>&2
  exit /b 1
)
.venv\Scripts\python.exe -m accusignals %*
exit /b %ERRORLEVEL%
