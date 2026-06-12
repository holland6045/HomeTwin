#!/bin/bash
# HomeTwin Linux server install — the central-coordinator deployment
# (target: headless NUC, optional NVIDIA GPU for ONNX inference).
#
#   HOMETWIN_CHANNEL=main ./install.sh          # CPU
#   HOMETWIN_GPU=1 ./install.sh                 # + onnxruntime-gpu (CUDA)
#
# Installs a venv under ~/.local/share/hometwin, a config under
# ~/.config/hometwin/, and a systemd *user* service. The same script
# re-run is the updater (or use the dashboard's Update button / update.sh).
set -euo pipefail

REPO_URL="${HOMETWIN_REPO:-https://github.com/holland6045/HomeTwin}"
CHANNEL="${HOMETWIN_CHANNEL:-main}"
APP="${HOMETWIN_HOME:-$HOME/.local/share/hometwin}"
CONF_DIR="$HOME/.config/hometwin"
UNIT_DIR="$HOME/.config/systemd/user"
VENV="$APP/venv"
EXTRA="vision"
[ "${HOMETWIN_GPU:-0}" = "1" ] && EXTRA="vision,ml-cuda"

command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }
command -v git >/dev/null || { echo "git required (updates install from the repo)" >&2; exit 1; }

mkdir -p "$APP" "$CONF_DIR" "$UNIT_DIR"

if [ ! -x "$VENV/bin/pip" ]; then
    echo "creating venv at $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
fi
echo "installing hometwin[$EXTRA] from $REPO_URL@$CHANNEL"
"$VENV/bin/pip" install --quiet --upgrade --force-reinstall \
    "hometwin[$EXTRA] @ git+${REPO_URL}@${CHANNEL}"
git ls-remote "$REPO_URL" "refs/heads/$CHANNEL" | cut -f1 > "$APP/installed-commit" || true

if [ ! -f "$CONF_DIR/config.yaml" ]; then
    cat > "$CONF_DIR/config.yaml" <<'YAML'
# HomeTwin server config. Full reference: configs/apartment.example.yaml
# in the repo. Cameras on a headless server are usually network streams
# (ESP32-CAM / RTSP); a local USB webcam is /dev/video0 -> device: 0.
world:
  zones:
    - {name: main-room, min: [0, 0, 0], max: [5, 4, 2.6]}
items:
  - id: keys
    name: House keys
    tags: ["aruco:7"]
sensors:
  - type: camera
    id: cam-1
    position: [1.5, 0.0, 2.2]
    yaw_deg: 90
    pitch_deg: 35
    surface_z: 0.0
    source: {type: opencv, device: 0, width: 1920, height: 1080, fps: 30, fourcc: MJPG}
    detector: {type: aruco, dictionary: DICT_4X4_250}
  - type: network_bridge
    id: bridge
    port: 8787
tracker:
  poll_hz: 10
  state_path: hometwin_state.json
api:
  host: 127.0.0.1
  port: 8080
YAML
    echo "wrote starting config to $CONF_DIR/config.yaml — edit it for this site"
fi

cat > "$UNIT_DIR/hometwin.service" <<EOF
[Unit]
Description=HomeTwin tracker (central coordinator)
After=network-online.target

[Service]
# the dashboard's Update button reinstalls from this channel in-process
Environment=HOMETWIN_REPO=${REPO_URL}
Environment=HOMETWIN_CHANNEL=${CHANNEL}
# relative config paths (state, models/) resolve here
WorkingDirectory=${APP}
ExecStart=${VENV}/bin/hometwin run -c ${CONF_DIR}/config.yaml
# dashboard Restart/Update exits the process; systemd brings it back up
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > "$APP/update.sh" <<EOF
#!/bin/bash
# Manual update: reinstall from the channel and bounce the service.
set -euo pipefail
"$VENV/bin/pip" install --upgrade --force-reinstall \\
    "hometwin[$EXTRA] @ git+${REPO_URL}@${CHANNEL}"
git ls-remote "$REPO_URL" "refs/heads/$CHANNEL" | cut -f1 > "$APP/installed-commit"
systemctl --user restart hometwin
EOF
chmod +x "$APP/update.sh"

systemctl --user daemon-reload
systemctl --user enable --now hometwin
# keep the service running when no one is logged in (headless NUC)
loginctl enable-linger "$USER" 2>/dev/null || true

echo
echo "HomeTwin is up: http://127.0.0.1:8080/"
echo "  config:  $CONF_DIR/config.yaml   (systemctl --user restart hometwin after edits)"
echo "  logs:    journalctl --user -u hometwin -f"
echo "  update:  $APP/update.sh   (or the dashboard's Update button)"
[ "${HOMETWIN_GPU:-0}" = "1" ] && \
    echo "  GPU:     verify with: curl -s localhost:8080/health | grep -o 'CUDA[A-Za-z]*'"
