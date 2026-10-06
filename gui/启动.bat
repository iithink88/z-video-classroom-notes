@echo off
cd /d "%~dp0"

REM Prefer the locally verified Python (deps ready); fall back to system python.
set "PY=C:\Users\lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Classroom Notes Agent ...
echo   A browser window will open automatically. Close this window to stop.
echo.

"%PY%" gui.py

echo.
echo The agent has stopped.
echo If it did not start, install Python 3.11+ with "Add Python to PATH", then re-run.
pause
