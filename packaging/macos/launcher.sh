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

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null; then
        PY="$p"; break
    fi
done
if [ -z "$PY" ]; then
    alert "No Python 3.9+ found. Install Apple's Command Line Tools (Terminal: xcode-select --install) or Python from python.org, then reopen."
    exit 1
fi

remote_commit() {
    git ls-remote "$REPO_URL" "refs/heads/$CHANNEL" 2>>"$LOG" | cut -f1
}

install_or_update() {
    local target="$1"
    say "installing hometwin@$CHANNEL ($target) with $("$VENV/bin/python" -V 2>&1)"
    if "$VENV/bin/pip" install --quiet --upgrade --force-reinstall \
            "hometwin[vision,ml] @ git+${REPO_URL}@${CHANNEL}" >>"$LOG" 2>&1; then
        echo "$target" > "$SUPPORT/installed-commit"
    else
        say "pip install failed — see log"
        return 1
    fi
}

# a venv built with the wrong/old interpreter (e.g. a stale 3.9 venv that
# rejected the package) is rebuilt from scratch
venv_ok() {
    [ -x "$VENV/bin/python" ] && "$VENV/bin/python" \
        -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 9) else 1)' 2>/dev/null
}
if [ -d "$VENV" ] && ! venv_ok; then
    say "rebuilding venv (incompatible interpreter)"
    rm -rf "$VENV"
fi

if [ ! -x "$VENV/bin/pip" ]; then
    say "first run: creating venv"
    "$PY" -m venv "$VENV" >>"$LOG" 2>&1
    "$VENV/bin/pip" install --quiet --upgrade pip >>"$LOG" 2>&1
    if ! install_or_update "$(remote_commit || echo unknown)"; then
        alert "HomeTwin install failed — see ~/Library/Logs/HomeTwin/hometwin.log"
        exit 1
    fi
    [ -f "$SUPPORT/autoupdate" ] || echo "true" > "$SUPPORT/autoupdate"
elif [ ! -x "$VENV/bin/hometwin" ]; then
    # venv exists but the package isn't installed (a prior failed install):
    # self-heal by reinstalling regardless of the recorded commit
    say "hometwin missing from venv — reinstalling"
    if ! install_or_update "$(remote_commit || echo unknown)"; then
        alert "HomeTwin install failed — see ~/Library/Logs/HomeTwin/hometwin.log"
        exit 1
    fi
elif [ "$(cat "$SUPPORT/autoupdate" 2>/dev/null)" = "true" ]; then
    REMOTE="$(remote_commit || true)"
    LOCAL="$(cat "$SUPPORT/installed-commit" 2>/dev/null || echo none)"
    if [ -n "$REMOTE" ] && [ "$REMOTE" != "$LOCAL" ]; then
        say "update available: $LOCAL -> $REMOTE"
        # stop a running tracker so the new code is live on next start
        if [ -f "$SUPPORT/hometwin.pid" ]; then
            kill "$(cat "$SUPPORT/hometwin.pid")" 2>/dev/null || true
            rm -f "$SUPPORT/hometwin.pid"
            sleep 1
        fi
        install_or_update "$REMOTE" || say "update failed, keeping $LOCAL"
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
