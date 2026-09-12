@echo off
rem Run i3pro straight from the source tree (no install, no network needed).
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
python -m i3pro %*
