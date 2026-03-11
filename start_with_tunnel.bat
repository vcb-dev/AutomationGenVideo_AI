@echo off
REM ============================================================
REM  VietChiBao AI Service + Cloudflare Tunnel
REM  Double-click để chạy. Tự động expose localhost ra internet.
REM
REM  Yêu cầu: cloudflared.exe phải có trong cùng thư mục hoặc PATH
REM  Tải tại: https://github.com/cloudflare/cloudflared/releases
REM           → cloudflared-windows-amd64.exe → đổi tên thành cloudflared.exe
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set PORT=8001
if not "%1"=="" set PORT=%1

echo.
echo ==========================================
echo   VietChiBao AI Service + Tunnel
echo   Port: %PORT%
echo ==========================================
echo.

REM ── Tìm cloudflared ────────────────────────────────────────
set CF_EXE=
if exist "%~dp0cloudflared.exe" (
    set CF_EXE=%~dp0cloudflared.exe
    echo [OK] Found cloudflared.exe in project folder
) else (
    where cloudflared >nul 2>&1
    if not errorlevel 1 (
        set CF_EXE=cloudflared
        echo [OK] Found cloudflared in PATH
    ) else (
        echo [ERROR] cloudflared.exe not found!
        echo.
        echo Please download from:
        echo https://github.com/cloudflare/cloudflared/releases/latest
        echo Download: cloudflared-windows-amd64.exe
        echo Rename to: cloudflared.exe
        echo Place in: %~dp0
        echo.
        pause
        exit /b 1
    )
)

REM ── Python và venv ─────────────────────────────────────────
set PYTHON_CMD=python
%PYTHON_CMD% --version >nul 2>&1
if errorlevel 1 (
    set PYTHON_CMD=py
    %PYTHON_CMD% --version >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Python not found in PATH
        pause
        exit /b 1
    )
)

if exist "venv\Scripts\activate.bat" (
    echo [OK] Activating virtual environment...
    call venv\Scripts\activate.bat
)

REM ── Migrations ─────────────────────────────────────────────
echo.
echo [1/4] Running migrations...
%PYTHON_CMD% manage.py migrate --noinput
if errorlevel 1 (
    echo [WARN] Migration had issues, continuing...
)

REM ── Celery Worker ──────────────────────────────────────────
echo.
echo [2/4] Starting Celery Worker (background)...
start "Celery Worker - AI" cmd /k "cd /d "%~dp0" && (if exist venv\Scripts\activate.bat call venv\Scripts\activate.bat) && celery -A core worker --loglevel=info --concurrency=2 --pool=threads -n worker@%%h"

REM ── Cloudflare Tunnel ──────────────────────────────────────
echo.
echo [3/4] Starting Cloudflare Tunnel...
echo      Waiting for tunnel URL...

REM Chạy tunnel, ghi log ra file tạm để detect URL
set TUNNEL_LOG=%TEMP%\vcb_tunnel_%PORT%.log
if exist "%TUNNEL_LOG%" del "%TUNNEL_LOG%"

start "Cloudflare Tunnel - AI" cmd /k ""%CF_EXE%" tunnel --url http://localhost:%PORT% --no-autoupdate > "%TUNNEL_LOG%" 2>&1"

REM Đợi tối đa 30 giây để URL xuất hiện trong log
set TUNNEL_URL=
set /a WAIT=0
:WAIT_LOOP
timeout /t 1 /nobreak >nul
set /a WAIT+=1

REM Tìm URL trong log file
if exist "%TUNNEL_LOG%" (
    for /f "tokens=*" %%L in ('findstr /i "trycloudflare.com" "%TUNNEL_LOG%"') do (
        REM Extract URL từ dòng log
        for %%W in (%%L) do (
            echo %%W | findstr /i "https://" >nul
            if not errorlevel 1 (
                set TUNNEL_URL=%%W
            )
        )
    )
)

if not "!TUNNEL_URL!"=="" goto FOUND_URL
if !WAIT! lss 30 goto WAIT_LOOP

REM Timeout - vẫn chạy nhưng không auto-detect được URL
echo.
echo [WARN] Could not auto-detect tunnel URL after 30s
echo        Check the "Cloudflare Tunnel" window for the URL
echo        It should look like: https://xxx-xxx.trycloudflare.com
echo.
goto START_DJANGO

:FOUND_URL
echo.
echo ==========================================
echo   TUNNEL URL DETECTED:
echo   !TUNNEL_URL!
echo ==========================================
echo.

REM Copy URL vào clipboard
echo !TUNNEL_URL! | clip
echo [OK] URL copied to clipboard!

REM Hiển thị hướng dẫn update GCP
echo.
echo ┌─────────────────────────────────────────────────┐
echo │  UPDATE GCP CLOUD RUN ENVIRONMENT:              │
echo │                                                 │
echo │  Cách 1 - Chạy script tự động (xem bên dưới)   │
echo │  Cách 2 - Manual qua GCP Console:               │
echo │    Service: vcb-ai                              │
echo │    ENV: AI_SERVICE_URL = !TUNNEL_URL!           │
echo │                                                 │
echo │  Hoặc cập nhật BE nếu BE gọi AI:               │
echo │    Service: vcb-be                              │  
echo │    ENV: AI_SERVICE_URL = !TUNNEL_URL!           │
echo └─────────────────────────────────────────────────┘
echo.

echo [INFO] Neu can hien thi lai huong dan update GCP, ban co the chay file update_gcp_tunnel_url.bat

:START_DJANGO
REM ── Django Server ──────────────────────────────────────────
echo.
echo [4/4] Starting Django AI Server...
echo.
echo ==========================================
if not "!TUNNEL_URL!"=="" (
    echo   LOCAL:  http://localhost:%PORT%
    echo   PUBLIC: !TUNNEL_URL!
) else (
    echo   LOCAL:  http://localhost:%PORT%
    echo   PUBLIC: Check Cloudflare Tunnel window
)
echo.
echo   NAS Access: VCB_MEDIA via LAN (direct)
echo   Press Ctrl+C to stop
echo ==========================================
echo.

%PYTHON_CMD% manage.py runserver 0.0.0.0:%PORT%

endlocal
