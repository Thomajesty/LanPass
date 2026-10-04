@echo off
rem ============================================================
rem  LAN Transfer - RECEIVER  (run this on the NEW computer)
rem  Opens the web UI at http://127.0.0.1:8899
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

set "PORT=8899"
echo ============================================================
echo   RECEIVER side  -  web UI will open in your browser
echo   http://127.0.0.1:%PORT%   (close this window to stop)
echo ============================================================
%PY% "%~dp0lan_transfer.py" pull --port %PORT%
pause
