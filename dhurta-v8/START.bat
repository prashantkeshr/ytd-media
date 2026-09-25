@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1 || (echo Python 3.10+ is required. Install Python and check "Add Python to PATH".&pause&exit /b 1)
for /f "skip=1 tokens=1" %%i in ('wmic process where "name='python.exe' and commandline like '%%app.py%%'" get processid 2^>nul') do if not "%%i"=="" taskkill /PID %%i /F >nul 2>&1
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade -r requirements.txt
.venv\Scripts\python.exe app.py
pause
