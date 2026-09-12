@echo off
rem Run i3pro straight from the source tree (no install, no network needed).
rem
rem   i3pro.cmd info i2pro_data\*.ld
rem   i3pro.cmd serve --data i2pro_data --open
rem
rem The launchers ??.bat / ????.bat call this, so Python discovery and
rem PYTHONPATH live in exactly one place.
rem
rem Candidate interpreters are probed by actually running i3pro: on some
rem machines "py -3" exists but has no interpreter registered, on others
rem "python" is a Microsoft Store stub. Probing the real command avoids both.
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"

set "PY_CMD="
for %%C in (python "py -3" python3) do (
  if not defined PY_CMD (
    %%~C -m i3pro --help >nul 2>nul
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

%PY_CMD% -m i3pro %*
endlocal & exit /b %ERRORLEVEL%
