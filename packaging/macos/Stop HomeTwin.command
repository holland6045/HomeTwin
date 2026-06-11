#!/bin/bash
set -euo pipefail
SUPPORT="$HOME/Library/Application Support/HomeTwin"
if [ -f "$SUPPORT/hometwin.pid" ]; then
    kill "$(cat "$SUPPORT/hometwin.pid")" 2>/dev/null || true
    rm -f "$SUPPORT/hometwin.pid"
    echo "tracker stopped"
else
    pkill -f "hometwin run" 2>/dev/null && echo "tracker stopped" || echo "not running"
fi
