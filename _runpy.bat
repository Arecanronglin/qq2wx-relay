@echo off
rem ============================================================
rem  Shared helper: check environment, then run qq2wx via run.py
rem  Usage from other .bat files:  call "%~dp0_runpy.bat" [args]
rem
rem  Keep this file ASCII-only. cmd runs on code page 936 here,
rem  so non-ASCII bytes in a .bat get mis-parsed.
rem ============================================================
setlocal
set "PY=%~dp0.venv\Scripts\python.exe"
set "ENTRY=%~dp0run.py"
rem Force UTF-8 for stdio AND for decoding the command line, so the
rem Chinese folder name in this path survives.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%PY%" goto :noenv
if not exist "%ENTRY%" goto :noentry
"%PY%" "%ENTRY%" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%

:noenv
echo.
echo [ERROR] Python venv not found:
echo         %PY%
echo.
echo The ".venv" folder is missing. Run these once in this folder:
echo         python -m venv .venv
echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
endlocal & exit /b 2

:noentry
echo.
echo [ERROR] Cannot find run.py under:
echo         %~dp0
echo         Make sure you run this from the qq2wx-relay folder.
echo.
endlocal & exit /b 2
