@echo off
REM Continuous scanner; restarts automatically if it ever exits (e.g. network drop).
setlocal
cd /d "%~dp0.."
:loop
call windows\accusignals.bat scan --loop --notify %*
echo [%date% %time%] scanner exited with %ERRORLEVEL%, restarting in 30s... 1>&2
timeout /t 30 /nobreak >nul
goto loop
