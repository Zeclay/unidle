#!/bin/bash
# Build a single-file macOS app bundle for Unidle.
# Run from a venv with requirements.txt installed.
# No --hidden-import flags needed for pywebview: PyInstaller's static
# analysis picks up settings_ui.py's `import webview` on its own.
set -e

python assets/generate_icon.py

pyinstaller --onefile --windowed --name Unidle --icon assets/icon.icns unidle.py

echo
echo "Build complete: dist/Unidle.app"
echo
echo "Note: to make this a tray-only app with no Dock icon, add a"
echo "LSUIElement key to dist/Unidle.app/Contents/Info.plist:"
echo
echo "  <key>LSUIElement</key><true/>"
echo
echo "Or pass --osx-bundle-identifier plus a custom Info.plist to"
echo "pyinstaller so it's baked in automatically on every build."
