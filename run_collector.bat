@echo off
chcp 65001 >nul
rem ------------------------------------------------------------------
rem Kakaopay live collector - always-on runner.
rem   * collect_live.py refreshes the "today" sheet tab every 10 minutes
rem     on clock slots (:00 :10 :20 ...)
rem   * if python exits for any reason, restart it after 60 seconds
rem   * logs go to logs\collect_YYYYMMDD.log (one file per day)
rem Scheduled task "KakaopayCollector" runs this at logon.
rem Comments are ASCII on purpose: a UTF-8 .bat with Korean text breaks cmd parsing.
rem ------------------------------------------------------------------
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

rem keep the log readable in any editor: python writes UTF-8 instead of the ANSI codepage
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0..\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

:loop
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TODAY=%%i"
set "LOG=logs\collect_%TODAY%.log"

echo. >> "%LOG%"
echo ===== %date% %time% collector start ===== >> "%LOG%"
"%PY%" -u collect_live.py >> "%LOG%" 2>&1
echo ===== %date% %time% collector stopped (exit %errorlevel%), restarting in 60s ===== >> "%LOG%"

timeout /t 60 /nobreak >nul
goto loop
