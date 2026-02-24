@echo off
REM Unified script to start Django + Celery for production-ready multi-user deployment
REM Usage: start.bat [port]
REM Example: start.bat 8001

setlocal

REM Default port matching NestJS configuration
set PORT=8001

REM Use first argument as port if provided
if not "%1"=="" set PORT=%1

echo ==========================================
echo  VietChiBao AI Service Startup
echo  Port: %PORT%
echo ==========================================
echo.

REM Navigate to script directory
cd /d "%~dp0"

REM Determine Python command
set PYTHON_CMD=python
%PYTHON_CMD% --version >nul 2>&1
if errorlevel 1 (
    py --version >nul 2>&1
    if errorlevel 1 (
        echo Error: Python is not installed or not in PATH
        pause
        exit /b 1
    )
    set PYTHON_CMD=py
)

echo Using Python: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

REM Activate virtual environment if exists
if exist "venv\Scripts\activate.bat" (
    echo Activating virtual environment...
    call venv\Scripts\activate.bat
)

REM Check if Django is installed
echo Checking Django installation...
%PYTHON_CMD% -c "import django" >nul 2>&1
if errorlevel 1 (
    echo Django not found. Installing dependencies...
    %PYTHON_CMD% -m pip install -r requirements.txt
) else (
    for /f "delims=" %%i in ('%PYTHON_CMD% -c "import django; print(django.get_version())"') do set DJANGO_VERSION=%%i
    echo Django found
)

REM Run migrations
echo.
echo Running migrations...
%PYTHON_CMD% manage.py migrate --noinput

REM ── Start Celery Worker (background window) ─────────────────────────────────
echo.
echo Starting Celery Worker (background)...
start "Celery Worker - AI Service" cmd /k "cd /d "%~dp0" && (if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat) && celery -A core worker --loglevel=info --concurrency=2 --pool=threads -n worker@%%h"

REM ── Start Celery Beat Scheduler (background window) ─────────────────────────
echo.
echo Starting Celery Beat Scheduler (background)...
start "Celery Beat - AI Service" cmd /k "cd /d "%~dp0" && (if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat) && celery -A core beat --loglevel=info"

REM Give Celery a moment to connect to Redis
timeout /t 2 /nobreak >nul

REM ── Start Django Server ─────────────────────────────────────────────────────
echo.
echo ==========================================
echo  Django Server: http://localhost:%PORT%
echo  Celery Worker: running in separate window
echo  Celery Beat:   running in separate window
echo  Press Ctrl+C to stop Django server
echo  (Close Celery windows separately)
echo ==========================================
echo.

%PYTHON_CMD% manage.py runserver 0.0.0.0:%PORT%
