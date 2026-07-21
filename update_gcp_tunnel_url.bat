@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set TUNNEL_URL=%~1
if "!TUNNEL_URL!"=="" (
    echo.
    set /p TUNNEL_URL="Nhap Cloudflare Tunnel URL (https://...): "
)

if "!TUNNEL_URL!"=="" (
    echo [ERROR] Ban chua nhap URL!
    pause
    exit /b 1
)

echo.
echo ===============================================================
echo   HUONG DAN UPDATE TUNNEL URL LEN GCP CONSOLE BANG TAY
echo ===============================================================
echo.
echo Vui long lam theo cac buoc sau:
echo.
echo 1. Nhin vao trinh duyet (se tu mo ngay sau day), dang nhap vao Google Cloud.
echo 2. Click vao service co ten la: vcb-be
echo 3. Nhin len tren cung, click vao chu "EDIT & DEPLOY NEW REVISION"
echo 4. Tim den the "Variables & Secrets" (Hoac "Container, Connections, Security")
echo 5. Tim den bien moi truong ten la: AI_SERVICE_URL
echo 6. Sua gia tri cua no thanh URL duoi day:
echo.
echo    !TUNNEL_URL!
echo.
echo    (Ghi chu: Minh da luu san vao trong Clipboard roi, ban chi can Ctrl+V la paste dc)
echo.
echo 7. Cuon xuong cuoi cung va nhat nut "DEPLOY" (hoac "Giao file"). Doi vai phut la xong!
echo.

echo !TUNNEL_URL! | clip

set /p OPEN_BROWSER="--- Nhan ENTER de mo loi tat GCP Console trong trinh duyet --- "
start https://console.cloud.google.com/run?project=project-bd91a0d7-ee2e-4b68-926

echo.
echo [OK] Vay la hoan tat. GCP BE se goi ve AI Service o may ban qua tunnel URL.
pause
endlocal
