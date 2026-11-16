#!/bin/sh
# Dorie Play for the Mac: dist/Dorie Play.app and dist/DoriePlay-mac.dmg (ad-hoc signed, not notarized).
# Run from the project folder with a Python that has requirements-window.txt and pyinstaller installed:
#   sh packaging/mac/build.sh [python]
set -eu
PYTHON=${1:-python3}
cd "$(dirname "$0")/../.."   # the project folder
export PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR=1   # a broken signature stops the build, not the user
PYTHONUTF8=1 "$PYTHON" -m PyInstaller packaging/dorieplay.spec --noconfirm --distpath dist --workpath build
APP="dist/Dorie Play.app"
codesign --verify --deep --strict "$APP"
"$APP/Contents/MacOS/DoriePlay" --self-test
ROOT=$(mktemp -d)
cp -R "$APP" "$ROOT/"
ln -s /Applications "$ROOT/Applications"
hdiutil create -quiet -volname "Dorie Play" -srcfolder "$ROOT" -ov -format UDZO dist/DoriePlay-mac.dmg
rm -rf "$ROOT"
echo "dist/DoriePlay-mac.dmg"
