@echo off
rem ============================================================
rem  LAN Transfer GUI  -  double click on BOTH computers,
rem  then pick "SENDER" on the old one and "RECEIVER" on the new one.
rem
rem  Optional:  start_gui.bat sender
rem             start_gui.bat receiver
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo.
  echo [错误] 没有找到 Python。
  echo        请到 https://www.python.org/downloads/windows/ 下载安装，
  echo        安装时务必勾选 "Add python.exe to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

echo 正在启动本地快传...
%PY% "%~dp0lanshare_gui.py" %*
if errorlevel 1 (
  echo.
  echo [程序退出，错误信息见上方]
  pause
)
