@echo off
setlocal
cd /d "%~dp0"
title Dhurta Media Lite - Stop

set APPDATA_DIR=%LOCALAPPDATA%\Dhurta Media
set PID_FILE=%APPDATA_DIR%\server.pid

if not exist "%PID_FILE%" (
    echo Dhurta Media server PID file not found.
    echo It may not be running, or was not started with START.bat.
    echo You can also close the START.bat window directly.
    pause & exit /b 0
)

set /p SERVER_PID=<"%PID_FILE%"
if "%SERVER_PID%"=="" (
    echo PID file is empty. Cleaning up.
    del "%PID_FILE%" 2>nul
    pause & exit /b 0
)

echo Stopping Dhurta Media server (PID %SERVER_PID%)...
taskkill /PID %SERVER_PID% /F >nul 2>&1
if errorlevel 1 (
    echo Process may have already stopped.
) else (
    echo Server stopped.
)

del "%PID_FILE%" 2>nul
pause
