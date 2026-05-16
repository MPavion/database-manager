@echo off
setlocal
cd /d "%~dp0"

echo Initializing Notion Local Sync Middleware...

set "PYTHON_EXE=venv\Scripts\python.exe"
set "NEEDS_INSTALL=0"

if /I "%~1"=="--refresh" set "NEEDS_INSTALL=1"

if not exist "%PYTHON_EXE%" (
    echo Creating virtual environment...
    py -3 -m venv venv 2>nul || python -m venv venv || goto :error
    set "NEEDS_INSTALL=1"
)

echo Activating virtual environment...
call venv\Scripts\activate.bat || goto :error

if not exist ".bootstrap_complete" set "NEEDS_INSTALL=1"

if "%NEEDS_INSTALL%"=="1" (
    echo Upgrading pip...
    python -m pip install --upgrade pip || goto :error

    echo Installing dependencies...
    pip install -r requirements.txt || goto :error

    > ".bootstrap_complete" echo ok
) else (
    echo Existing environment detected. Skipping dependency reinstall.
    echo Use setup_and_run.bat --refresh if you want to refresh packages.
)

echo Launching Application...
call run_app.bat
goto :end

:error
echo.
echo Setup failed. Please review the messages above and try again.
pause
exit /b 1

:end
endlocal
