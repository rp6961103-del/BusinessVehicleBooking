@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.11+ and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if not exist ".env" copy /Y ".env.example" ".env" >nul
if not exist ".env" (
  echo Could not create .env
  pause
  exit /b 1
)
echo.
echo Setup complete. Edit .env with your MySQL password, then run start_windows.bat
pause
