@echo off
setlocal
cd /d "%~dp0"
echo =============================================
echo Dhurta Media Lite - Repair / Update
echo =============================================
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install --upgrade --force-reinstall -r requirements.txt
.venv\Scripts\python.exe -c "import imageio_ffmpeg,subprocess; p=imageio_ffmpeg.get_ffmpeg_exe(); print('FFmpeg:',p); r=subprocess.run([p,'-version']); raise SystemExit(r.returncode)"
if errorlevel 1 echo WARNING: FFmpeg self-test failed. Check Windows Security / antivirus and run System Check in Dhurta.
pause
