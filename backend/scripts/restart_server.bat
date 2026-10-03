@echo off
set "PID_TO_KILL=%~1"

if not "%PID_TO_KILL%"=="" (
    ping 127.0.0.1 -n 2 >nul
    taskkill /F /PID %PID_TO_KILL% >nul 2>&1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_helper.ps1"
exit /b 0
