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
rem CAN raw-frame logs live in their own folder; pass both so those sessions show
rem up in the sidebar too (serve --data can be repeated).
set "CAN_DIR=%~dp0can_data"

echo.
echo  ==============================================================
echo    i3pro  -  telemetry workbench (.ld / csv / xlsx)
echo  ==============================================================
echo    data folder : %DATA_DIR%
if exist "%CAN_DIR%" echo    can  folder : %CAN_DIR%
echo.
echo    Keep this window open while you work.
echo    Press Ctrl-C or close the window when you are done.
echo  ==============================================================
echo.

if exist "%CAN_DIR%" (
  call "%~dp0i3pro.cmd" serve --data "%DATA_DIR%" --data "%CAN_DIR%" --host 0.0.0.0 --open
) else (
  call "%~dp0i3pro.cmd" serve --data "%DATA_DIR%" --host 0.0.0.0 --open
)
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo  --------------------------------------------------------------
  echo    [ERROR] i3pro exited with code %RC%
  echo  --------------------------------------------------------------
  echo    Most likely causes:
  echo      1. The data folder above has no log files.  i3pro reads .ld / .csv
  echo         / .xlsx / .txt / .tsv -- put your logs in i2pro_data, or edit
  echo         DATA_DIR at the top of this file.
  echo      2. A runtime package is missing.  i3pro.cmd now says so, and prints
  echo         the exact pip command -- or just double-click the install-deps
  echo         bat in this folder.  Chinese instructions: README.md, section
  echo         "Getting started from a fresh clone".
  echo      3. Anything else: run   i3pro.cmd --help   and read the last lines.
  echo.
  pause
)
endlocal
