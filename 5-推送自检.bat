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
echo Push channel self-test (send to your PHONE)
echo.
echo Sends one test message using the push settings in config.yaml.
echo This does NOT touch the WeChat desktop client at all.
echo.
echo Setup (3 minutes, one time):
echo   1. Scan the QR at https://www.pushplus.plus/ with WeChat
echo   2. Copy your token from the web page
echo   3. Put it in config.yaml under wechat.push.token
echo   4. Set wechat.driver to push
echo.
echo Then run this and check your phone.
echo ============================================================
echo.
pause
call "%~dp0_runpy.bat" --probe-push
set "RC=%ERRORLEVEL%"
echo.
echo Exit code: %RC%
pause
exit /b %RC%
