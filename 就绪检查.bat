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
echo READINESS CHECK - run this first
echo.
echo Tells you in one glance whether the relay can start, and
echo exactly what is still missing if it cannot.
echo.
echo Checks: WeChat version, update blocking, window visibility,
echo login state, UIA tree, NapCat connection, group membership,
echo WebSocket port, send channel, forwarding rules.
echo.
echo Expect:  all green once you have logged in to WeChat.
echo ============================================================
echo.
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    pause
    exit /b 2
)
"%PY%" "%~dp0tools\ready_check.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%   (0 = all passed)
pause
exit /b %RC%
