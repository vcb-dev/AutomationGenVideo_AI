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

REM ── Chọn Python: LUÔN ưu tiên venv theo ĐƯỜNG DẪN TUYỆT ĐỐI ────────────────
REM Trước đây script đặt PYTHON_CMD=python rồi mới call activate.bat. Nếu activate
REM không ăn (chạy từ shell khác, PATH bị ghi đè...) thì `python` rơi về Python hệ
REM thống — thiếu PyJWT nên Django sập, server vẫn "chạy" nhưng trả 500 cho MỌI
REM request. Lỗi này từng khiến toàn bộ tính năng AI chết 2 ngày mà không ai biết.
REM Gọi thẳng venv\Scripts\python.exe thì không thể im lặng rơi nhầm nữa.
if exist "venv\Scripts\python.exe" (
    set PYTHON_CMD=venv\Scripts\python.exe
) else (
    echo.
    echo ============================================================
    echo  CANH BAO: khong tim thay venv\Scripts\python.exe
    echo  Dang dung Python he thong — RAT DE THIEU GOI va sap ngam.
    echo  Tao venv:  python -m venv venv
    echo             venv\Scripts\python.exe -m pip install -r requirements.txt
    echo ============================================================
    echo.
    set PYTHON_CMD=python
)

echo Using Python: %PYTHON_CMD%
%PYTHON_CMD% --version
echo.

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

REM ── Kiểm tra trước khi chạy: Django phải import được toàn bộ cấu hình ────────
REM Nếu thiếu gói (vd PyJWT cho core.authentication) thì DỪNG NGAY kèm thông báo
REM rõ ràng, thay vì khởi động 1 server hỏng trả 500 cho mọi endpoint.
echo Kiem tra cau hinh Django truoc khi chay...
%PYTHON_CMD% -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings');django.setup();import core.urls" 2>nul
if errorlevel 1 (
    echo.
    echo ============================================================
    echo  LOI: Django khong khoi tao duoc — server se tra 500 o MOI
    echo  endpoint neu van chay. Thuong do thieu goi trong venv.
    echo.
    echo  Chi tiet loi:
    %PYTHON_CMD% -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','core.settings');django.setup();import core.urls"
    echo.
    echo  Cach sua:  %PYTHON_CMD% -m pip install -r requirements.txt
    echo ============================================================
    pause
    exit /b 1
)
echo Cau hinh Django OK.

REM Run migrations
echo.
echo Running migrations...
%PYTHON_CMD% manage.py migrate --noinput

REM IMPORTANT: Close any existing Celery Worker/Beat windows before restart,
REM or old workers will keep running with stale config.
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
