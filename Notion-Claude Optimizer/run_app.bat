@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Virtual environment not found. Run setup_and_run.bat first.
    pause
    exit /b 1
)

rem Call the venv interpreter directly: activate.bat hard-codes the folder path
rem and silently falls back to system Python if the project folder is renamed.
echo Starting Notion-Claude Optimizer...
echo No terminal input is required. The app opens in the system tray near the clock.
"%~dp0venv\Scripts\python.exe" -m src.main

endlocal
