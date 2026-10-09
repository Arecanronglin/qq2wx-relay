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
echo WeChat automation health check
echo.
echo Checks the registry flag, the main window, its visibility,
echo and whether the UIA control tree is actually exposed.
echo.
echo Safe: only ShowWindow is called. No clicks, no typing,
echo no messages sent, no memory writes.
echo.
echo Run this FIRST whenever the relay stops working.
echo ============================================================
echo.
pause
call "%~dp0_runpy.bat" --wx-health
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
