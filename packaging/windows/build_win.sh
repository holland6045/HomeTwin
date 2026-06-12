#!/bin/bash
# Assemble dist/HomeTwin-win from the templates in this directory.
#
#   REPO_URL=https://github.com/holland6045/RGBDesk \
#   CHANNEL=claude/apartment-tracking-cv-app-9n823r \
#   packaging/windows/build_win.sh
#
# Same thin-launcher model as HomeTwin.app: double-clicking HomeTwin.bat
# creates a venv under %LOCALAPPDATA%\HomeTwin, pip-installs
# hometwin[vision] from REPO_URL@CHANNEL, and self-updates whenever the
# channel branch moves (disable: echo false > %LOCALAPPDATA%\HomeTwin\autoupdate).
# Runs anywhere bash does, so CI can verify the package; only Windows can
# run the result.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO_URL="${REPO_URL:-https://github.com/holland6045/RGBDesk}"
CHANNEL="${CHANNEL:-main}"
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
OUT="${OUT:-$ROOT/dist}"
PKG="$OUT/HomeTwin-win"

rm -rf "$PKG"
mkdir -p "$PKG"

bake() {
    sed -e "s|__REPO_URL__|$REPO_URL|g" -e "s|__CHANNEL__|$CHANNEL|g" "$1" > "$2"
}
# cmd.exe wants CRLF in batch files
bake_bat() {
    sed -e "s|__REPO_URL__|$REPO_URL|g" -e "s|__CHANNEL__|$CHANNEL|g" -e 's|$|\r|' "$1" > "$2"
}

for ps1 in launcher.ps1 update.ps1 stop.ps1 logs.ps1; do
    bake "$HERE/$ps1" "$PKG/$ps1"
done
for bat in "HomeTwin.bat" "Update HomeTwin.bat" "Stop HomeTwin.bat" "HomeTwin Logs.bat"; do
    bake_bat "$HERE/$bat" "$PKG/$bat"
done

cp "$ROOT/configs/webcam-quickstart.yaml" "$PKG/default-config.yaml"
echo "$VERSION" > "$PKG/version.txt"

echo "built $PKG (repo=$REPO_URL channel=$CHANNEL version=$VERSION)"
echo "ship it: (cd $OUT && zip -r HomeTwin-win.zip HomeTwin-win)"
