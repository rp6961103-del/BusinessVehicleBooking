@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Run setup_windows.bat first.
  pause
  exit /b 1
)
if not exist ".env" (
  echo .env not found.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
echo Starting Business Vehicle Booking with Live Vehicle Location Tracking...
python run_live_tracking.py
pause
