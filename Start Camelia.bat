@echo off
setlocal
set "CAMELIA_ROOT=%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CAMELIA_ROOT%scripts\start-camelia.ps1" %*

if errorlevel 1 (
    echo.
    echo Camelia did not start successfully. See the message above.
    pause
)

endlocal
