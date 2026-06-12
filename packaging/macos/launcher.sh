#!/bin/bash
# HomeTwin.app launcher: owns a venv under Application Support, self-updates
# from the configured git channel, starts the tracker, opens the dashboard.
# Designed for the test/iterate phase: relaunching the app IS the update.
set -euo pipefail

REPO_URL="${HOMETWIN_REPO:-__REPO_URL__}"
DEFAULT_CHANNEL="__CHANNEL__"
PORT=8080

SUPPORT="$HOME/Library/Application Support/HomeTwin"
LOGS="$HOME/Library/Logs/HomeTwin"
VENV="$SUPPORT/venv"
CONFIG="$SUPPORT/config.yaml"
LOG="$LOGS/hometwin.log"
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"

mkdir -p "$SUPPORT" "$LOGS"
CHANNEL="$(cat "$SUPPORT/channel" 2>/dev/null || echo "$DEFAULT_CHANNEL")"
echo "$CHANNEL" > "$SUPPORT/channel"

say() { echo "[hometwin-app] $*" >> "$LOG"; }
alert() {
    osascript -e "display alert \"HomeTwin\" message \"$1\"" >/dev/null 2>&1 || true
}

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    alert "python3 not found. Install Apple's Command Line Tools first: open Terminal and run: xcode-select --install"
    exit 1
fi

remote_commit() {
    git ls-remote "$REPO_URL" "refs/heads/$CHANNEL" 2>>"$LOG" | cut -f1
}

install_or_update() {
    local target="$1"
    say "installing hometwin@$CHANNEL ($target)"
    "$VENV/bin/pip" install --quiet --upgrade --force-reinstall \
        "hometwin[vision,ml] @ git+${REPO_URL}@${CHANNEL}" >>"$LOG" 2>&1
    echo "$target" > "$SUPPORT/installed-commit"
}

if [ ! -x "$VENV/bin/pip" ]; then
    say "first run: creating venv"
    "$PY" -m venv "$VENV" >>"$LOG" 2>&1
    "$VENV/bin/pip" install --quiet --upgrade pip >>"$LOG" 2>&1
    install_or_update "$(remote_commit || echo unknown)"
    [ -f "$SUPPORT/autoupdate" ] || echo "true" > "$SUPPORT/autoupdate"
elif [ "$(cat "$SUPPORT/autoupdate" 2>/dev/null)" = "true" ]; then
    REMOTE="$(remote_commit || true)"
    LOCAL="$(cat "$SUPPORT/installed-commit" 2>/dev/null || echo none)"
    if [ -n "$REMOTE" ] && [ "$REMOTE" != "$LOCAL" ]; then
        say "update available: $LOCAL -> $REMOTE"
        install_or_update "$REMOTE"
        # restart a running tracker so the new code is live
        if [ -f "$SUPPORT/hometwin.pid" ]; then
            kill "$(cat "$SUPPORT/hometwin.pid")" 2>/dev/null || true
            rm -f "$SUPPORT/hometwin.pid"
            sleep 1
        fi
    fi
fi

if [ ! -f "$CONFIG" ]; then
    cp "$RES/default-config.yaml" "$CONFIG"
    say "wrote default config to $CONFIG"
fi

if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    say "tracker already running"
else
    say "starting tracker"
    # fixed cwd: relative config paths (state, models/) resolve here.
    # The Update button env tells the tracker where to reinstall from.
    cd "$SUPPORT"
    HOMETWIN_REPO="$REPO_URL" HOMETWIN_CHANNEL="$CHANNEL" \
        nohup "$VENV/bin/hometwin" run -c "$CONFIG" >>"$LOG" 2>&1 &
    echo $! > "$SUPPORT/hometwin.pid"
    for _ in $(seq 1 30); do
        curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
        sleep 0.5
    done
    if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        alert "HomeTwin failed to start. Log: ~/Library/Logs/HomeTwin/hometwin.log (camera permission? device index? see config at $CONFIG)"
        exit 1
    fi
fi

open "http://127.0.0.1:$PORT/"
