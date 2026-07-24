@echo off
REM Build a single-file Windows executable for Unidle.
REM Run from a venv with requirements.txt installed.
REM No --hidden-import flags needed for pywebview: PyInstaller's static
REM analysis picks up settings_ui.py's `import webview` on its own.

python assets\generate_icon.py

REM Use "python -m PyInstaller" rather than the "pyinstaller" entry point:
REM the module form always resolves against the same Python that is on
REM PATH, which avoids "'pyinstaller' is not recognized" when the venv's
REM Scripts folder isn't visible to the .bat's own environment.
python -m PyInstaller --onefile --noconsole --name Unidle --icon assets\icon.ico unidle.py
if errorlevel 1 (
    echo.
    echo Build FAILED - see the error above.
    exit /b 1
)

echo.
echo Build complete: dist\Unidle.exe
