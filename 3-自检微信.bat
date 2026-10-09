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
echo WeChat channel self-test
echo.
echo Connects to WeChat and sends ONE test message to
echo the File Transfer Helper. No group is disturbed.
echo.
echo !! From now on, DO NOT touch the mouse or keyboard !!
echo It simulates clicks on the WeChat window.
echo ============================================================
echo.
pause
call "%~dp0_runpy.bat" --probe-wx
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
