@echo off
setlocal
cd /d "%~dp0"

echo.
echo  Notion Local Sync
echo  -----------------

set "PYTHON_EXE=venv\Scripts\python.exe"
set "NEEDS_INSTALL=0"
set "RUN_WIZARD=0"

if /I "%~1"=="--refresh" set "NEEDS_INSTALL=1"
if /I "%~1"=="--wizard"  set "RUN_WIZARD=1"

if not exist "%PYTHON_EXE%" (
    echo  Creating virtual environment...
    py -3 -m venv venv 2>nul || python -m venv venv 2>nul || goto :no_python
    set "NEEDS_INSTALL=1"
)

call venv\Scripts\activate.bat 2>nul || goto :error

if not exist ".bootstrap_complete" set "NEEDS_INSTALL=1"

if "%NEEDS_INSTALL%"=="1" (
    echo  Installing dependencies ^(this takes a minute on first run^)...
    python -m pip install --upgrade pip -q || goto :error
    pip install -r requirements.txt -q || goto :error
    > ".bootstrap_complete" echo ok
    echo  Dependencies installed.
)

rem Run the setup wizard if this is a fresh install (no .env) or --wizard flag passed
if not exist ".env" set "RUN_WIZARD=1"

if "%RUN_WIZARD%"=="1" (
    echo.
    python setup_wizard.py
    goto :end
)

echo  Launching app...
call run_app.bat
goto :end

:no_python
echo.
echo  ERROR: Python was not found on this system.
echo  Please install Python 3.11 or later from:
echo    https://www.python.org/downloads/
echo  During installation, check "Add Python to PATH".
echo.
pause
exit /b 1

:error
echo.
echo  Setup failed. Please review the messages above and try again.
pause
exit /b 1

:end
endlocal
