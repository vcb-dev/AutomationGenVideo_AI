@echo off
REM Unified script to start Django development server on Windows
REM Usage: start.bat [port]
REM Example: start.bat 8000

setlocal

REM Default port
set PORT=8000

REM Use first argument as port if provided
if not "%1"=="" set PORT=%1

echo ==========================================
echo Starting Django Development Server
echo ==========================================
echo.

REM Navigate to script directory
cd /d "%~dp0"

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH
    pause
    exit /b 1
)

echo Using Python:
python --version
echo.

REM Activate virtual environment if exists
if exist "venv\Scripts\activate.bat" (
    echo Activating virtual environment...
    call venv\Scripts\activate.bat
)

REM Check if Django is installed
echo Checking Django installation...
python -c "import django" >nul 2>&1
if errorlevel 1 (
    echo Django not found. Installing dependencies...
    python -m pip install -r requirements.txt
) else (
    for /f "delims=" %%i in ('python -c "import django; print(django.get_version())"') do set DJANGO_VERSION=%%i
    echo Django !DJANGO_VERSION! found
)

REM Run migrations
echo.
echo Running migrations...
python manage.py migrate --noinput

REM Start server
echo.
echo ==========================================
echo Server starting on http://localhost:%PORT%
echo Press Ctrl+C to stop
echo ==========================================
echo.

python manage.py runserver 0.0.0.0:%PORT%
