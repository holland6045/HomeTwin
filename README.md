# Apartment Tracker

Modular CV + sensor-fusion system that answers one question: **where are my
keys / wallet / phone right now?**

It fuses whatever sensing you have — cameras, BLE beacons, RF tomography
meshes, MCU sensor nodes — into a single per-item position estimate with a
zone name ("kitchen counter"), an uncertainty radius, and a freshness age.
The core is pure Python (stdlib + PyYAML), so it runs on a laptop, a
Raspberry Pi, or any always-on box; hardware support is plugins all the way
down.

```
┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐
│ camera +     │  │ BLE scanner │  │ RF tomography│  │ MCU nodes (ESP32,│
│ detector     │  │ (RSSI)      │  │ link mesh    │  │ RP2040, ...)     │
└──────┬──────┘  └──────┬──────┘  └──────┬───────┘  └──────┬──────────┘
       │ Position        │ Range          │ Area            │ JSON lines/TCP
       ▼                 ▼                ▼                  ▼
   ┌────────────────────────────────────────────────────────────┐
   │            Observation stream (3 canonical shapes)         │
   └──────────────────────────────┬─────────────────────────────┘
                                  ▼
                  ┌────────────────────────────────┐
                  │ Fusion engine: per-item Kalman │
                  │ filter (EKF for ranges),       │
                  │ identity + gating, multilater- │
                  │ ation init, staleness          │
                  └───────────────┬────────────────┘
                                  ▼
                  ┌────────────────────────────────┐
                  │ World model (zones) + HTTP API │
                  │ "wallet: sofa, ±0.3 m, 12s ago"│
                  └────────────────────────────────┘
```

## Quick start

```bash
pip install -e .            # core: stdlib + PyYAML only
pip install -e .[vision]    # optional: OpenCV cameras + ArUco
pip install -e .[ml]        # optional: ONNX object detection

# zero-hardware demo: synthetic apartment with all three modalities
apartment-tracker simulate

# same demo as a live dashboard with togglable sensor overlays
apartment-tracker simulate --serve   # then open http://127.0.0.1:8080/

# real deployment
apartment-tracker run -c configs/apartment.example.yaml
apartment-tracker where keys
# -> House keys: kitchen_counter at (6.5, 1.0, 0.9) ±0.06 m, seen 2.0s ago via cam-kitchen

apartment-tracker plugins   # list everything installed
```

Run the tests with `pytest` (44 tests, no hardware or heavy deps needed).

## Design

### Hardware agnosticism: three canonical observations

Every sensing modality reduces to one of three shapes before fusion
(`apartment_tracker/observations.py`):

| Shape | Carries | Produced by |
|---|---|---|
| `PositionObservation` | 3D fix + sigma | camera+detector, UWB, anything with geometry |
| `RangeObservation` | distance from a known anchor | BLE RSSI, UWB, acoustic ranging |
| `BearingObservation` | sight ray from a known origin | ray-mode cameras, directional antennas |
| `AreaObservation` | diffuse centroid + spread | RF tomography, PIR zones, pressure mats |

The fusion engine only knows these shapes. Adding a sensing modality never
touches fusion code.

### Identity: items and tags

Items are registered once; tags map sensor-level identities onto them —
`aruco:7`, `ble:AA:BB:CC:DD:EE:FF`, `rfid:<EPC>`, or anonymous detector
labels (`keys`). One item can carry several tags across modalities, and tags
can be added at runtime (`POST /items/keys/tags`) — manual tagging without
restart. Anonymous label detections are Mahalanobis-gated against the
existing track so a look-alike object across the room can't hijack it.

### Fusion

Per item: a 6-state (position+velocity) Kalman filter, pure-Python.
Position/area observations are linear updates; ranges and bearings are EKF
updates (azimuth/elevation measurement model for rays). Range-only tracks
are seeded by coarse-grid multilateration at item height — ceiling-mounted
(coplanar) anchors otherwise leave z unobservable and a naive init
converges to the mirror solution above the ceiling. Uncertainty grows when
nothing reports; estimates go `stale`, never silently wrong.

**Multi-camera 3D (`mode: ray`):** overlapping cameras emit sight rays
instead of assuming a surface plane. Rays are buffered per item and
triangulated (least-squares closest point, with a viewpoint-diversity
check) to seed the track; after that every frame from every camera is an
EKF refinement, so two cameras watching the same room track an item moving
through free space — in the bundled simulation the carried phone tracks to
~2 cm where BLE alone managed ~1.7 m. A single ray-mode camera degrades
gracefully to an item-height depth prior. Camera sight-lines are drawn on
the dashboard map ("Camera rays" layer).

### Visualization: fusion overlays

The API serves a dashboard (`GET /`) with two render targets fed by the
same layer data:

- **Map view** — top-down apartment: zones, fused item estimates with
  uncertainty circles, BLE range rings (each sphere intersected with item
  height), the tomography occupancy heat map with mesh node positions,
  presence blob, and camera poses.
- **Camera view** (one tab per camera) — the same layers projected into
  that camera's pixel space via the shared pinhole geometry, composited
  over the camera's live MJPEG stream (`stream_url` in camera config).
  You see the heat map and BLE rings *on the video*.

Every layer is individually toggleable (persisted in the browser). The
server only assembles JSON (`/overlay/map`, `/overlay/camera/<id>`);
rendering is client-side canvas, so the core stays dependency-free. Any
sensor can publish a drawable layer by implementing `overlay()` — the UI
picks it up without changes.

**Path traces:** the tracker keeps a bounded motion trail per item
(new point on every ≥0.15 m move); trails render in all three views with
per-item colors and age fading.

**3D view:** the dashboard's 3D tab renders the full overlay set — item
markers with uncertainty spheres, trails, BLE rings, camera rays, the
tomography heat map, zones, presence — as a Three.js scene composited
*inside* the Gaussian-splat scan of the apartment when
`world.splat_asset` is configured (aligned via `world.splat_transform`),
or over a ground grid otherwise. Renderer is lazy-loaded client-side; the
tracker host never pays for it.

**Spots & identical items:** `world.spots` mark micro-locations — an
individual drawer, shelf, or bin — so answers read "desk-drawer-2" instead
of "office"; a tagged spot's surveyed position doubles as a camera
calibration anchor. `item_sets` bulk-register families of visually
identical items (storage boxes) distinguished only by sequential tags;
anonymous sightings of the shared label refine the gated nearest existing
track and can never seed or hijack one. `apartment-tracker make-tag`
prints designed fiducial labels (dark plate, neon accent, hazard stripes,
mono ID type — functional ArUco core in vector SVG).
`apartment-tracker snapshot-map` renders the live map overlay to SVG for
headless previews.

**Calibration anchors (optional):** printed ArUco blocks at surveyed
positions (`apartment-tracker make-anchor`, `world.anchors` in config)
give every camera that sees one a shared fixed reference: drift is
detected and reported (drifted cameras render red), and with
`anchor_correct: true` rotation drift self-heals online, bounded to ±10°
from the configured pose. Cameras without an anchor in view are
unaffected. Details: `docs/calibration-anchors.md`.

**Camera auto-calibration:** `apartment-tracker calibrate-cameras` derives
each fixed camera's `position/yaw_deg/pitch_deg/hfov_deg` from the COLMAP
reconstruction produced by the splat scan — include one snapshot per camera
in the scan's image set and supply two reference points; no tape measure.

**Scheduled scan refresh:** `scripts/rebuild_splat.sh` runs the full loop
on a GPU host (capture snapshots → train → recalibrate → push): the new
scan is hot-swapped into the running tracker via `apartment-tracker
update-splat` and open dashboards reload it automatically. Cron it weekly.
Details and workflow: `docs/gaussian-splatting.md`.

### Persistence & history

With `tracker.state_path` set, tracks are saved atomically (periodic +
shutdown) and restored on start with their original timestamps — after a
reboot the answer is still "wallet: sofa, 2 h ago [stale]" instead of
"never seen". Zone transitions are recorded as events (`GET /events`):
"keys moved kitchen_counter → hall at 18:42".

### Plugins (`apartment_tracker/registry.py`)

Four kinds: `sensor`, `detector`, `frame_source`, `trainer`. Config selects
by name; third-party packages self-register via the
`apartment_tracker.plugins` entry-point group. Optional heavy deps (cv2,
onnxruntime) are imported only inside the plugin that needs them — the core
import graph stays clean, and a missing backend errors only if config
actually selects it.

Built in today:

- **sensors**: `camera` (any frame source × any detector, pinhole projection
  onto known surface planes), `ble_scanner` (log-distance path-loss RSSI
  ranging), `rf_tomography` (ellipse-weighted back-projection over an RF
  link mesh — presence sensing with no wearable), `network_bridge` (JSON
  lines over TCP from any MCU), `scripted` (tests/replay)
- **detectors**: `aruco` (printable fiducials — cheapest reliable tag),
  `onnx` (any YOLO-style exported model, COCO-pretrained or self-trained)
- **frame sources**: `opencv` (USB webcam, RTSP/MJPEG URL — ESP32-CAM
  works out of the box), `static` (files/replay)
- **trainers**: `path_loss` (closed-form BLE calibration fit),
  `detector_finetune` (wraps any external training command; output ONNX
  slots back in via config)

### MCU integration

MCUs stay dumb and replaceable: anything that can open a TCP socket and
print one JSON object per line participates via the `network_bridge` sensor
(full contract in `docs/mcu-integration.md`, reference ESP32 BLE-scanner
node in `firmware/esp32-ble-scanner/`):

```json
{"type": "rssi", "sensor_id": "esp32-hall", "mac": "AA:BB:CC:DD:EE:FF",
 "anchor": [0.0, 4.0, 2.2], "rssi": -67}
```

Timestamps are assigned on receipt (MCU clocks are never trusted), malformed
lines are counted and dropped (a flaky node can't take the tracker down),
and raw RSSI is converted host-side with the trainable path-loss model — so
recalibration never requires reflashing firmware.

### Retraining

The system is calibrated/retrained from recorded data, per modality:

1. `DatasetRecorder` captures labeled samples (RSSI at known distances,
   detection crops marked correct/incorrect) as JSONL.
2. `apartment-tracker train path_loss` fits the BLE model to *your* walls
   and furniture (closed-form, instant).
3. `apartment-tracker train detector_finetune` shells out to any training
   stack (ultralytics, a cloud job) and drops an ONNX file that the existing
   `onnx` detector loads via config. The heavy ML stack is never a
   dependency of the tracker itself.
4. RF tomography recalibrates in-place (`calibrate()` re-baselines link RSS
   after furniture moves).

### Security

Full model in `docs/security.md`. Designed to cost one constant-time
comparison per request or connection — fusion and polling are untouched
(measured: auth overhead below request-latency noise).

- **API**: bearer tokens with `viewer` (read) and `admin` (read+write)
  roles; secrets from env vars or 0600 files, never config literals.
  The dashboard prompts for a token on first 401 and remembers it.
  With no tokens configured, reads stay open but writes are loopback-only.
- **MCU bridge**: optional connection-level shared secret (first line
  `{"auth": "..."}`), 64 KiB line cap, host-side timestamps so nodes
  can't poison history. Plain TCP by design — segment the IoT VLAN or
  tunnel via WireGuard for hostile networks.
- **TLS**: deliberately delegated to a reverse proxy (caddy/nginx) in
  front of the localhost-bound API.

## Repository layout

```
apartment_tracker/
  observations.py      # the 3 canonical observation shapes + Detection
  registry.py          # plugin registry (the extension seam)
  world.py             # zones over the world frame; point -> zone name
  items.py             # item registry, multi-modality tags, runtime tagging
  linalg.py            # tiny dense linear algebra (no numpy needed)
  fusion/
    kalman.py          # 6-state KF + EKF range update
    engine.py          # identity resolution, gating, multilateration init
  sensors/             # camera, ble, tomography, network bridge, mock
  detectors/           # aruco, onnx
  training/            # dataset capture + trainer plugins
  config.py            # YAML -> world + items + sensor fleet
  tracker.py           # poll/fuse orchestrator loop, zone events, trails
  store.py             # atomic state persistence across restarts
  overlay.py           # map + per-camera overlay assembly for the UI
  calibration.py       # COLMAP reconstruction -> camera poses
  static/ui.html       # canvas dashboard + 3D splat view with overlays
  api.py               # stdlib HTTP API + dashboard
  simulate.py          # full synthetic apartment (demo + e2e tests)
  cli.py
configs/apartment.example.yaml
docs/mcu-integration.md
firmware/esp32-ble-scanner/   # reference MCU node (PlatformIO)
tests/                 # 50 tests, hardware-free
```

## Extending without breaking anything

- **New sensor hardware**: implement `SensorAdapter.poll() -> [Observation]`,
  decorate with `@register("sensor", "my_sensor")`, name it in YAML. Done.
- **New camera type**: just a `frame_source` plugin; detectors and
  projection are reused.
- **New detection model**: export to ONNX, point config at it — or add a
  `detector` plugin for a different runtime.
- **New modality that doesn't fit position/range/area**: add an observation
  type and one `isinstance` branch in the fusion engine; existing sensors
  and configs are untouched.
- **Out-of-tree plugins**: publish a package exposing the
  `apartment_tracker.plugins` entry point; no fork needed.
