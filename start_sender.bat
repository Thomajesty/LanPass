@echo off
rem ============================================================
rem  LAN Transfer - SENDER  (run this on the OLD computer)
rem  Usage: drag a folder onto this file, or just double-click
rem         to share this program folder itself.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

set "PY=python"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.8+ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  set "PY=py -3"
)

set "ROOT=%~1"
if "%ROOT%"=="" set "ROOT=%~dp0"
set "PORT=8788"

echo ============================================================
echo   SENDER side  -  sharing: %ROOT%
echo   port: %PORT%   (press Enter to skip the token)
echo ============================================================
set /p TOKEN=Access token (optional): 

echo.
if "%TOKEN%"=="" (
  %PY% "%~dp0lan_transfer.py" serve --root "%ROOT%" --port %PORT%
) else (
  %PY% "%~dp0lan_transfer.py" serve --root "%ROOT%" --port %PORT% --token "%TOKEN%"
)
pause
