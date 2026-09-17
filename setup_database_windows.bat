@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python setup_database.py
if errorlevel 1 (
  echo.
  echo Database setup failed. Check MySQL is running and verify MYSQL_USER and MYSQL_PASSWORD in .env.
  pause
  exit /b 1
)
pause
