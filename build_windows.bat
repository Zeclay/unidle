@echo off
REM Build a single-file Windows executable for Unidle.
REM Run from a venv with requirements.txt installed.
REM No --hidden-import flags needed for pywebview: PyInstaller's static
REM analysis picks up settings_ui.py's `import webview` on its own.

python assets\generate_icon.py

pyinstaller --onefile --noconsole --name Unidle --icon assets\icon.ico unidle.py

echo.
echo Build complete: dist\Unidle.exe
