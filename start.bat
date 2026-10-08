@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found.
    echo Create it with: py -3.13 -m venv .venv
    echo Then run: .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)
if not exist "data\logs" mkdir "data\logs"
set "LOGFILE=data\logs\server-console.log"
echo.>>"%LOGFILE%"
echo ==================================================>>"%LOGFILE%"
echo %date% %time% START>>"%LOGFILE%"
echo ==================================================>>"%LOGFILE%"
echo [START] Vintage Story Modpack Builder
 echo [START] Console log: %CD%\%LOGFILE%
echo [START] Opening http://127.0.0.1:8000
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOGFILE%' -Append"
echo.>>"%LOGFILE%"
echo %date% %time% STOP>>"%LOGFILE%"
pause
