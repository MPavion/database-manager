@echo off
setlocal
cd /d "%~dp0"

echo Building Notion-Claude Optimizer Launcher...

if not exist ".venv\Scripts\python.exe" (
    echo Root virtual environment not found. Create .venv first, then run this again.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat || exit /b 1
python -m pip install --upgrade pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "Notion-Claude Optimizer Launcher" notion_claude_optimizer_launcher.py || exit /b 1

echo.
echo Done. Your launcher is here:
echo %cd%\dist\Notion-Claude Optimizer Launcher.exe
echo.
pause
endlocal
