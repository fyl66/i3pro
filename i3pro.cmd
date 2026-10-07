@echo off
rem Run i3pro straight from the source tree: no pip install, no build step, no
rem network -- once the four runtime packages are present (see "probe 2" below).
rem
rem   i3pro.cmd info i2pro_data\*.ld
rem   i3pro.cmd serve --data i2pro_data --open
rem
rem The launcher bats (start / snapshot / import) all call this, so Python
rem discovery, the dependency check and PYTHONPATH live in exactly one place.
rem Wording here is ASCII on purpose: the console code page on Chinese Windows
rem varies, so the Chinese messages are printed by Python, not by cmd.exe.
rem
rem Two separate probes on purpose.  "There is no Python at all" and "there is a
rem Python but a package is missing" are different problems with different
rem fixes; conflating them used to tell the reader to install Python when all
rem they needed was one pip command.  Guards: tests/test_i3pro.py :: TestBootstrap.
rem
rem Candidate interpreters are probed by actually running them: on some machines
rem "py -3" exists but has no interpreter registered, on others "python" is a
rem Microsoft Store stub.  Probing the real command avoids both.
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"

rem --- probe 1: is there a Python at all? ------------------------------------
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
  exit /b 9009
)

rem --- probe 2: are the four runtime packages there? -------------------------
rem Without this, a missing numpy makes "python -m i3pro --help" fail and the
rem script above would have reported "no Python found" -- a wrong diagnosis that
rem costs the reader a whole Python reinstall.
%PY_CMD% -c "import numpy, pandas, pyarrow, openpyxl" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python %PY_CMD% found, but the runtime packages are missing.
  echo.
  echo   Run exactly one of these, then start again:
  echo.
  echo       %PY_CMD% -m pip install -r requirements.txt
  echo.
  echo   ...or double-click the install-deps bat in this folder.
  echo.
  echo   Without internet access on this machine, copy a wheel folder over
  echo   and use:   %PY_CMD% -m pip install --no-index --find-links DIR -r requirements.txt
  echo.
  exit /b 9009
)

%PY_CMD% -m i3pro %*
endlocal & exit /b %ERRORLEVEL%
