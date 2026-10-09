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
echo Real-data end-to-end test
echo.
echo Pulls REAL messages from NapCat (includes real image/file
echo segments) and runs them through the whole pipeline with a
echo fake sender. Downloads the real media too.
echo.
echo Needs NapCat running. Does NOT need WeChat login.
echo Expect:  about 18 passed / 0 failed
echo ============================================================
echo.
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    pause
    exit /b 2
)
"%PY%" "%~dp0tools\test_e2e_real.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%   (0 = all passed)
pause
exit /b %RC%
