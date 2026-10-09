@echo off
rem ----------------------------------------------
rem  Auto-generated launcher. ASCII only, by design.
rem  Chinese UI text is printed by Python instead.
rem  Regenerate with:  python tools\gen_bats.py
rem ----------------------------------------------
rem Switch console to UTF-8 so it matches Python's output encoding.
rem Safe here because this file is pure ASCII.
chcp 65001 >nul 2>&1
echo ============================================================
echo Full diagnostic (one-stop health check)
echo.
echo Checks ALL of these at once and tells you how to fix each:
echo   - config file, send channel, watched groups
echo   - NapCat / QQ login, group membership, WebSocket port
echo   - WeChat version, window, login state, UIA tree
echo   - whether the target WeChat group name actually exists
echo.
echo Run this whenever forwarding stops working.
echo Read-only except for ShowWindow (restores a hidden window).
echo ============================================================
echo.
pause
call "%~dp0_runpy.bat" --doctor
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
