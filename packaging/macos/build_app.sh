#!/bin/bash
# Assemble dist/HomeTwin.app from the templates in this directory.
#
#   REPO_URL=https://github.com/holland6045/RGBDesk \
#   CHANNEL=claude/apartment-tracking-cv-app-9n823r \
#   packaging/macos/build_app.sh
#
# The bundle is a thin launcher: on first open it creates a venv under
# ~/Library/Application Support/HomeTwin and pip-installs hometwin[vision,ml]
# from REPO_URL@CHANNEL; on every open it self-updates when the channel
# branch has new commits (disable: echo false > '~/Library/Application
# Support/HomeTwin/autoupdate'). Runs anywhere bash does, so CI can verify
# the bundle structure; only macOS can run the result.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
REPO_URL="${REPO_URL:-https://github.com/holland6045/RGBDesk}"
CHANNEL="${CHANNEL:-main}"
VERSION="${VERSION:-$(date +%Y.%m.%d)}"
OUT="${OUT:-$ROOT/dist}"
APP="$OUT/HomeTwin.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

sed "s|__VERSION__|$VERSION|g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"

bake() {
    sed -e "s|__REPO_URL__|$REPO_URL|g" -e "s|__CHANNEL__|$CHANNEL|g" "$1" > "$2"
    chmod +x "$2"
}
bake "$HERE/launcher.sh" "$APP/Contents/MacOS/HomeTwin"
bake "$HERE/Update HomeTwin.command" "$APP/Contents/Resources/Update HomeTwin.command"
bake "$HERE/Stop HomeTwin.command" "$APP/Contents/Resources/Stop HomeTwin.command"
bake "$HERE/HomeTwin Logs.command" "$APP/Contents/Resources/HomeTwin Logs.command"

cp "$ROOT/configs/webcam-quickstart.yaml" "$APP/Contents/Resources/default-config.yaml"

# ad-hoc signature keeps Gatekeeper to a right-click-Open on first launch
if command -v codesign >/dev/null 2>&1; then
    codesign --force --deep -s - "$APP" 2>/dev/null || true
fi

echo "built $APP (repo=$REPO_URL channel=$CHANNEL version=$VERSION)"
echo "ship it: zip -r HomeTwin.app.zip dist/HomeTwin.app"
