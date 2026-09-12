@echo off
rem ===========================================================================
rem  i3pro data import  --  drag .ld / .ldx files (or a folder) onto this file.
rem
rem  Copies them into i2pro_data so they show up in the workbench.  Nothing is
rem  uploaded anywhere: it is a local file copy, and an existing file with the
rem  same name is never overwritten (it gets -1, -2, ... appended).
rem
rem  You can also double-click it with no argument to see usage.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title i3pro import
chcp 65001 >nul 2>nul

if "%~1"=="" (
  echo.
  echo   ==============================================================
  echo     i3pro  -  import logs
  echo   ==============================================================
  echo     Drag one or more .ld / .ldx files (or a whole folder)
  echo     onto this file to copy them into:
  echo.
  echo       %~dp0i2pro_data
  echo.
  echo     Command line equivalent:
  echo       i3pro.cmd import "D:\logs\2026-09-xx.ld" --data i2pro_data
  echo       i3pro.cmd import "D:\logs" --data i2pro_data        ^(????^)
  echo       i3pro.cmd import "D:\logs\a.ld" --move              ^(???????^)
  echo   ==============================================================
  echo.
  pause
  exit /b 0
)

echo.
call "%~dp0i3pro.cmd" import %* --data "%~dp0i2pro_data"
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo   [ERROR] import exited with code %RC%  (see above)
pause
endlocal
