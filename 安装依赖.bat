@echo off
rem ===========================================================================
rem  i3pro dependency installer  --  double-click this file once per machine.
rem
rem  Installs the four runtime packages listed in requirements.txt
rem  (numpy / pandas / pyarrow / openpyxl) and then checks that they import.
rem  This is the only i3pro step that needs internet access; after it the
rem  workbench runs fully offline.
rem
rem  Wording is ASCII on purpose: the console code page on Chinese Windows
rem  varies, so the Chinese guidance lives in README.md and in Python's output.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title i3pro install dependencies
chcp 65001 >nul 2>nul

if not exist "%~dp0requirements.txt" (
  echo.
  echo [ERROR] requirements.txt is not next to this file.
  echo.
  echo   You are probably running this outside a full checkout of i3pro.
  echo   Clone or download the whole repository, then run it again.
  echo.
  pause
  endlocal & exit /b 2
)

set "PY_CMD="
for %%C in (python "py -3" python3) do (
  if not defined PY_CMD (
    %%~C -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY_CMD=%%~C"
  )
)
if not defined PY_CMD (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if not defined PY_CMD if exist "%%~fD\python.exe" set "PY_CMD=%%~fD\python.exe"
  )
)
if not defined PY_CMD (
  echo.
  echo [ERROR] Could not find a working Python 3.
  echo.
  echo   Install Python 3.10 or newer from https://www.python.org/downloads/
  echo   and tick "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  endlocal & exit /b 9009
)

echo.
echo   Interpreter : %PY_CMD%
echo   Packages    : %~dp0requirements.txt
echo.
echo   Installing ...  the first run needs internet access.
echo.

%PY_CMD% -m pip install -r "%~dp0requirements.txt"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [ERROR] pip exited with code %RC%.
  echo   Check the network and the proxy settings, then run this file again.
  echo.
  pause
  endlocal & exit /b %RC%
)

%PY_CMD% -c "import numpy, pandas, pyarrow, openpyxl; print('    numpy', numpy.__version__, '| pandas', pandas.__version__, '| pyarrow', pyarrow.__version__, '| openpyxl', openpyxl.__version__)"
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo   [ERROR] pip reported success but the packages still do not import.
  echo   See README.md, section: Getting started from a fresh clone.
  echo.
  pause
  endlocal & exit /b %RC%
)

echo.
echo   Done.  Close this window, then double-click the start bat to begin.
echo.
pause
endlocal
