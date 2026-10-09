@echo off
rem ----------------------------------------------
rem  Auto-generated script launcher. ASCII only, by design.
rem  Regenerate with:  python tools\gen_bats.py
rem ----------------------------------------------
chcp 65001 >nul 2>&1
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo ============================================================
echo Send-API binding check (static, sends nothing)
echo.
echo Verifies the parameters we pass to wx4py really match its
echo method signatures - catches the kind of TypeError that would
echo otherwise only appear on the first real send.
echo.
echo Also checks fallback handling for older wxauto-style names
echo (send_msg / send_files taking who/msg).
echo.
echo Expect:  30 passed / 0 failed
echo ============================================================
echo.
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    pause
    exit /b 2
)
"%PY%" "%~dp0tools\test_send_binding.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%   (0 = all passed)
pause
exit /b %RC%
