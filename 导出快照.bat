@echo off
rem ===========================================================================
rem  i3pro snapshot export  --  double-click this file.
rem
rem  Writes one self-contained HTML per session into the out\ folder, plus an
rem  index page, then opens it.  Those HTML files need no Python, no server and
rem  no network -- mail one to a team mate and it just opens.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title i3pro snapshot export
chcp 65001 >nul 2>nul

set "DATA_DIR=%~dp0i2pro_data"
if not exist "%DATA_DIR%" set "DATA_DIR=%~dp0."

echo.
echo   Exporting snapshots from:
echo     %DATA_DIR%
echo.

call "%~dp0i3pro.cmd" snapshot --data "%DATA_DIR%" --out "%~dp0out" --open
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [ERROR] snapshot export exited with code %RC%  (see above)
  echo.
  pause
)
endlocal
