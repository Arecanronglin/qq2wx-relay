@echo off
rem ============================================================
rem  Downgrade WeChat to 4.1.8.28 and block auto-update.
rem  MUST run as Administrator.
rem
rem  TEMPLATE file (ASCII). Generated into a GBK .bat by
rem  tools\gen_bats.py -- cmd runs on code page 936 here, so
rem  non-ASCII bytes in a .bat get mis-parsed.
rem
rem  NOTE: name deliberately does NOT start with an underscore,
rem  because tools/_*.txt is used as a scratch-file cleanup glob
rem  and once ate this template.
rem ============================================================
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  WeChat 4.1.8.28 downgrade + auto-update block
echo ============================================================
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator rights required.
    echo         Right-click this file, choose "Run as administrator".
    echo.
    pause
    exit /b 1
)
echo [OK] Running as Administrator.
echo.

rem The Python tool finds the installer by itself (registry + drive scan).
rem If it cannot, pass it explicitly:
rem     --installer "D:\path\to\WeChatWin_4.1.8.28.exe"
rem or set the environment variable WECHAT_INSTALLER.

echo This will:
echo   1. close WeChat
echo   2. silently install WeChat 4.1.8.28
echo   3. disable its updater so it cannot auto-upgrade again
echo.
echo You will need to log in to WeChat again afterwards.
echo.
pause

".venv\Scripts\python.exe" tools\fix_wechat_version.py --install
set "RC=%ERRORLEVEL%"

echo.
echo ============================================================
if "%RC%"=="0" (
    echo  DONE - WeChat downgraded and update-blocked.
    echo.
    echo  Now open WeChat, log in, then run:  0-wx-health.bat
) else (
    echo  FAILED with exit code %RC% - read the messages above.
)
echo ============================================================
echo.
pause
exit /b %RC%
