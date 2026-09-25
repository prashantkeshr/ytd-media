@echo off
setlocal
cd /d "%~dp0"
title Dhurta Media Lite - Repair

echo =============================================
echo  Dhurta Media Lite - Repair
echo =============================================
echo  This rebuilds the Python environment and
echo  reinstalls all dependencies.
echo  Your downloaded media files are NOT affected.
echo =============================================
echo.

:: Check Python
where py >nul 2>&1
if errorlevel 1 (
    echo  Python 3.10+ is required.
    echo  Download from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    pause & exit /b 1
)

:: Remove and recreate virtual environment
if exist .venv (
    echo Removing old virtual environment...
    rmdir /s /q .venv
)

echo Creating new virtual environment...
py -3 -m venv .venv
if errorlevel 1 (
    echo  Failed to create virtual environment.
    pause & exit /b 1
)

echo Upgrading pip...
.venv\Scripts\python.exe -m pip install --upgrade pip

echo Installing dependencies...
.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall -r requirements.txt
if errorlevel 1 (
    echo  Dependency installation failed. Check your internet connection.
    pause & exit /b 1
)

echo.
echo Testing media engine (FFmpeg)...
.venv\Scripts\python.exe -c "import imageio_ffmpeg, subprocess; p = imageio_ffmpeg.get_ffmpeg_exe(); print('FFmpeg:', p); r = subprocess.run([p, '-version'], capture_output=True); print('FFmpeg OK' if r.returncode == 0 else 'FFmpeg test FAILED')"
if errorlevel 1 (
    echo  WARNING: FFmpeg self-test failed.
    echo  Check Windows Security / antivirus - it may have quarantined ffmpeg.
    echo  Allow it, then run System Check inside Dhurta Media.
) else (
    echo Media engine OK.
)

echo.
echo =============================================
echo  Repair complete. Run START.bat to launch.
echo =============================================
pause
