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
echo LIVE RUN:  QQ groups  -^>  WeChat groups
echo.
echo Before running, make sure:
echo   1. NapCat is running and QQ is logged in
echo   2. WeChat is logged in and its window is NOT minimized
echo   3. config.yaml has  driver: wx4py
echo.
echo Avoid using the mouse/keyboard on the WeChat window.
echo.
echo To stop: press Ctrl+C
echo ============================================================
echo.
pause
call "%~dp0_runpy.bat" --live
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
