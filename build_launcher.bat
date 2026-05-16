@echo off
setlocal
cd /d "%~dp0"

echo Building Database Manager Launcher...

if not exist ".venv\Scripts\python.exe" (
    echo Root virtual environment not found. Create .venv first, then run this again.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat || exit /b 1
python -m pip install --upgrade pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Database Manager Launcher" database_manager_launcher.py || exit /b 1

echo.
echo Done. Your launcher is here:
echo %cd%\dist\Database Manager Launcher.exe
echo.
pause
endlocal
