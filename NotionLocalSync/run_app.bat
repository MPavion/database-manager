@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Virtual environment not found. Run setup_and_run.bat first.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat || exit /b 1

echo Starting Notion Local Sync...
echo No terminal input is required. The app opens in the system tray near the clock.
python -m src.main

endlocal
