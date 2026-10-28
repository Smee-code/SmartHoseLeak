@echo off
echo ============================================
echo   Smart Hose Leak Detection System - Setup
echo ============================================
echo.

:: Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download it from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

:: Create virtual environment if it doesn't exist
if not exist "venv" (
    echo [1/4] Creating virtual environment...
    python -m venv venv
) else (
    echo [1/4] Virtual environment already exists.
)

:: Activate virtual environment
echo [2/4] Activating virtual environment...
call venv\Scripts\activate.bat

:: Install dependencies
echo [3/4] Installing dependencies...
pip install -r requirements.txt

:: Run migrations
echo [4/4] Running database migrations...
python manage.py migrate

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo   To start the server, run:
echo     venv\Scripts\activate
echo     python manage.py runserver
echo.
pause
