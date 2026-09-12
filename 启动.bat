@echo off
rem ===========================================================================
rem  i3pro one-click launcher  --  double-click this file.
rem
rem  Starts the local workbench and opens it in your browser.  Leave this
rem  window open while you use it; close it (or press Ctrl-C) when finished.
rem
rem  Text is ASCII on purpose: the console code page on Chinese Windows varies,
rem  so all Chinese wording is printed by Python, which handles UTF-8 properly.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title i3pro workbench
chcp 65001 >nul 2>nul

set "DATA_DIR=%~dp0i2pro_data"
if not exist "%DATA_DIR%" set "DATA_DIR=%~dp0."

echo.
echo  ==============================================================
echo    i3pro  -  MoTeC .ld data workbench
echo  ==============================================================
echo    data folder : %DATA_DIR%
echo.
echo    Keep this window open while you work.
echo    Press Ctrl-C or close the window when you are done.
echo  ==============================================================
echo.

call "%~dp0i3pro.cmd" serve --data "%DATA_DIR%" --host 0.0.0.0 --open
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo  --------------------------------------------------------------
  echo    [ERROR] i3pro exited with code %RC%
  echo  --------------------------------------------------------------
  echo    Most likely causes:
  echo      1. The data folder above has no .ld files.
  echo         Put your logs in i2pro_data, or edit DATA_DIR in this file.
  echo      2. Python is missing a package.  Try:
  echo           i3pro.cmd --help
  echo         and see the ???? section of README.md.
  echo.
  pause
)
endlocal
