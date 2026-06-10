#!/usr/bin/env bash
# Weekly Gaussian-splat rebuild, run on the GPU host PC.
#   cron:    0 4 * * 1  /path/to/rebuild_splat.sh
#   systemd: OnCalendar=Mon *-*-* 04:00
#
# Requires: apartment-tracker (pip), nerfstudio (or swap in OpenSplat /
# Polycam export), and network access to the tracker API.
set -euo pipefail

TRACKER_HOST="${TRACKER_HOST:-127.0.0.1}"
TRACKER_PORT="${TRACKER_PORT:-8080}"
CONFIG="${CONFIG:-configs/apartment.yaml}"
REFPOINTS="${REFPOINTS:-scans/refpoints.yaml}"
WORK="${WORK:-$HOME/splat-scans/$(date +%F)}"

mkdir -p "$WORK/images"

# 1. fresh snapshots from every fixed camera (also re-calibrates them below);
#    drop in additional walkthrough photos/video frames for better coverage:
#    ffmpeg -i walkthrough.mp4 -vf fps=2 "$WORK/images/walk_%04d.jpg"
apartment-tracker capture-snapshots -c "$CONFIG" -o "$WORK/images"

# 2. SfM + splat training (any pipeline that emits .ply + a COLMAP model)
ns-process-data images --data "$WORK/images" --output-dir "$WORK/processed"
ns-train splatfacto --data "$WORK/processed" --output-dir "$WORK/model" \
    --viewer.quit-on-train-completion True
CONFIG_YML=$(find "$WORK/model" -name config.yml | head -1)
ns-export gaussian-splat --load-config "$CONFIG_YML" --output-dir "$WORK"

# 3. re-derive camera poses from the same reconstruction (drift check: diff
#    against current config before applying)
apartment-tracker calibrate-cameras \
    --colmap "$WORK/processed/colmap/sparse/0" \
    --pairs "$REFPOINTS" > "$WORK/cameras.yaml"

# 4. hot-swap the running tracker's scan — no restart, dashboards reload
apartment-tracker update-splat "$WORK/splat.ply" \
    --host "$TRACKER_HOST" --port "$TRACKER_PORT"

echo "rebuilt $(date): $WORK"
