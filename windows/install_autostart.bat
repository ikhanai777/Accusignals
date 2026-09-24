@echo off
REM Registers a Windows Task Scheduler task that starts the dashboard at logon.
REM Usage: windows\install_autostart.bat            (this PC only)
REM        windows\install_autostart.bat --host 0.0.0.0   (phone access too)
REM Remove with: schtasks /Delete /TN "Accusignals Dashboard" /F
setlocal
cd /d "%~dp0.."
set ROOT=%CD%
schtasks /Create /TN "Accusignals Dashboard" /SC ONLOGON /RL LIMITED /F ^
  /TR "cmd /c start \"Accusignals\" /min \"%ROOT%\windows\dashboard.bat\" %*" || exit /b 1
echo Installed. It will start at your next logon. Start it now with:  schtasks /Run /TN "Accusignals Dashboard"
