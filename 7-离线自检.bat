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
echo Offline self-test of the whole pipeline
echo.
echo Tests filtering, de-duplication, aggregation, chunking,
echo rate limiting, media handling and archiving - using a FAKE
echo sender. It does NOT touch WeChat or QQ, so it is safe to run
echo anytime, even before logging in.
echo.
echo Expect:  36 passed / 0 failed
echo ============================================================
echo.
if not exist "%PY%" (
    echo [ERROR] Python venv not found: %PY%
    pause
    exit /b 2
)
"%PY%" "%~dp0tools\test_pipeline_offline.py" %*
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%   (0 = all passed)
pause
exit /b %RC%
