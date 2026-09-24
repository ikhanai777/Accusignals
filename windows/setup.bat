@echo off
REM One-time setup on Windows 10: creates .venv, installs dependencies, checks Binance.
setlocal
cd /d "%~dp0.."
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'" || (
  echo Install Python 3.10+ from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".
  exit /b 1
)
if not exist .venv\Scripts\python.exe %PY% -m venv .venv || exit /b 1
.venv\Scripts\python.exe -m pip install --upgrade pip >nul
.venv\Scripts\python.exe -m pip install -r requirements.txt pytest || exit /b 1
.venv\Scripts\python.exe -m accusignals health
exit /b %ERRORLEVEL%
