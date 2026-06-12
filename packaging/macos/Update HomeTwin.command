#!/bin/bash
# Manual update: force-reinstall from the configured channel and restart.
set -euo pipefail
SUPPORT="$HOME/Library/Application Support/HomeTwin"
REPO_URL="${HOMETWIN_REPO:-__REPO_URL__}"
CHANNEL="$(cat "$SUPPORT/channel" 2>/dev/null || echo "__CHANNEL__")"
echo "updating hometwin from $CHANNEL ..."
"$SUPPORT/venv/bin/pip" install --upgrade --force-reinstall \
    "hometwin[vision,ml] @ git+${REPO_URL}@${CHANNEL}"
git ls-remote "$REPO_URL" "refs/heads/$CHANNEL" | cut -f1 > "$SUPPORT/installed-commit"
if [ -f "$SUPPORT/hometwin.pid" ]; then
    kill "$(cat "$SUPPORT/hometwin.pid")" 2>/dev/null || true
    rm -f "$SUPPORT/hometwin.pid"
    echo "tracker stopped — relaunch HomeTwin.app to start the new version"
fi
echo "done. installed: $(cat "$SUPPORT/installed-commit")"
