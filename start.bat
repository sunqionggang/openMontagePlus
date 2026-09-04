@echo off
cd /d D:\github\OpenMontage-dev
echo ============================================
echo   OpenMontage Server
echo   Open http://127.0.0.1:8000/prototype in browser
echo   Press Ctrl+C to stop
echo ============================================
echo.
set PY=C:\Users\91311\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
echo Using python: %PY%
"%PY%" --version
echo.
echo Starting server...
"%PY%" -m uvicorn om.server:app --port 8000 --log-level info
echo.
echo Server stopped. Press any key to close.
pause >nul
