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
echo Stage 1: QQ group  -^>  relay  (dry-run)
echo Nothing is sent to WeChat; output is printed here only.
echo.
echo NapCat must already be running and QQ logged in.
echo Ask ANOTHER QQ account to send messages in the group.
echo Wait up to 60 seconds for the summary to appear.
echo.
echo To stop: press Ctrl+C
echo ============================================================
echo.
pause
call "%~dp0_runpy.bat" --driver dry-run
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
