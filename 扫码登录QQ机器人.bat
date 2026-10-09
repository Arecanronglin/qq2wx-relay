@echo off
chcp 65001 >nul
title QQ Bot Login Helper
cd /d "%~dp0"

REM  <- change BOT_UIN to your bot QQ number (only used in the hint text below)
REM  <- change NAPCAT_DIR if you installed NapCat somewhere else
set BOT_UIN=123456789
set NAPCAT_DIR=D:\NapCat
set WEBUI=http://127.0.0.1:6099

netstat -ano | findstr ":3001" | findstr LISTENING >nul
if not errorlevel 1 (
  echo [OK] QQ bot is already online ^(port 3001 is listening^).
  echo      Next step: double-click the launcher numbered  4  (formal run^).
  echo.
  pause
  exit /b 0
)

echo ============================================================
echo  QQ bot is NOT online.  Port 3001 is not listening.
echo  This usually means QQ needs to be logged in again.
echo ============================================================
echo.
echo  Scan the QR code with the PHONE QQ of the bot account:
echo      %BOT_UIN%   (nickname: next-door)
echo.
echo  Do NOT use your main account. NapCat injects into QQ.exe
echo  and QQ may flag it as a plugin. Use a throwaway account.
echo.
pause

echo [1/3] Starting NapCat ...
start "" /min cmd /c "%NAPCAT_DIR%\launcher-user.bat"

echo [2/3] Waiting for the NapCat WebUI ...
set WEBUI_OK=0
for /l %%i in (1,1,20) do (
  timeout /t 3 /nobreak >nul
  powershell -NoProfile -Command "try{ $r=Invoke-WebRequest '%WEBUI%' -TimeoutSec 3 -UseBasicParsing; if($r.StatusCode -eq 200){exit 0}else{exit 1} }catch{ exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    echo       WebUI is up after %%i try^(ies^).
    set WEBUI_OK=1
    goto :open_panel
  )
)
:open_panel
if "%WEBUI_OK%"=="0" (
  echo       WebUI did not come up. Check that %NAPCAT_DIR% exists.
  echo       You can also open the QR image directly:
  echo           %NAPCAT_DIR%\cache\qrcode.png
  pause
  exit /b 1
)

echo       Opening %WEBUI% ...
start "" "%WEBUI%"
start "" "%NAPCAT_DIR%\cache\qrcode.png"
echo.
echo [3/3] Waiting for port 3001. Scan the QR code now.
echo       ^(the QR refreshes every ~30s, always scan the newest one^)
echo.
for /l %%i in (1,1,60) do (
  timeout /t 5 /nobreak >nul
  netstat -ano | findstr ":3001" | findstr LISTENING >nul
  if not errorlevel 1 (
    echo.
    echo [OK] Port 3001 is listening. QQ bot is ONLINE.
    echo      The relay program reconnects by itself - do NOT restart it.
    echo.
    echo      Next: run the launchers numbered 0 ^(health check^) then 4 ^(formal run^).
    echo.
    pause
    exit /b 0
  )
  echo       ... still waiting ^(%%i/60^)
)

echo.
echo [FAIL] Port 3001 did not come up within 5 minutes.
echo        Make sure the QR code was scanned and confirmed on the phone.
echo        If QQ shows a "plugin detected" warning, read section 5 of the
echo        handoff document in this folder BEFORE logging in again.
echo.
pause
exit /b 1
