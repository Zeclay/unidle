#!/bin/bash
# Build a single-file macOS app bundle for Unidle.
# Run from a venv with requirements.txt installed.
# No --hidden-import flags needed for pywebview: PyInstaller's static
# analysis picks up settings_ui.py's `import webview` on its own.
set -e

python assets/generate_icon.py

pyinstaller --onefile --windowed --name Unidle --icon assets/icon.icns unidle.py

# Make it a menu-bar-only (agent) app: LSUIElement stops macOS from ever
# giving it a Dock icon. Baking it into Info.plist here means there is no
# Dock icon to flash and get stuck on launch — the runtime activation-policy
# call in unidle.py is only a fallback for running from source.
plutil -replace LSUIElement -bool true dist/Unidle.app/Contents/Info.plist \
  || plutil -insert LSUIElement -bool true dist/Unidle.app/Contents/Info.plist

echo
echo "Build complete: dist/Unidle.app (menu-bar-only, no Dock icon)"
