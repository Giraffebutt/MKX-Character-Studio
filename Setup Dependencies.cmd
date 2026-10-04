@echo off
setlocal
cd /d "%~dp0"
echo This creates a private Python environment in this edition only.
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Ready. Double-click Launch Studio.cmd.
pause
exit /b 0
:failed
echo Setup failed. Install Python 3.10 or newer with Tcl/Tk, then run this again.
pause
exit /b 1
