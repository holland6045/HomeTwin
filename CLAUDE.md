# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this repo is

Two codebases, one repo. Almost all work happens in the first.

| Path | What | Language |
|---|---|---|
| `hometwin/` | **HomeTwin** — sensor-fused digital twin of an apartment: locates tagged items, people, and door/drawer state in real time, serves a dashboard | Python 3.9+ |
| `firmware/` | RGBDesk MCU nodes (currently one ESP32 BLE scanner that feeds HomeTwin) | C++17 / PlatformIO |

Firmware, protocol, LED/display/sensor-driver rules: **`docs/firmware.md`** — read
it only when touching `firmware/`. Don't load it for HomeTwin work.

## Commands

```bash
python3 -m pytest tests/ -q              # full suite (~220 tests, seconds)
python3 -m pytest tests/test_ble_mesh.py -q
pip install -e ".[vision,ml,dev]"        # cv2 + onnxruntime + pytest
hometwin run -c configs/webcam-quickstart.yaml   # dashboard on :8080
hometwin simulate                        # no hardware needed
pio run -d firmware/esp32-ble-scanner    # firmware (note the -d)
```

Tests that need `cv2`/`numpy` skip cleanly when those aren't installed.

## Where things live

Go straight to the file — don't grep the tree.

| Need to change | File |
|---|---|
| Observation shapes (the fusion contract) | `hometwin/observations.py` |
| EKF, track lifecycle, gating, people | `hometwin/fusion/engine.py`, `fusion/kalman.py` |
| Multi-anchor triangulation (BLE/UWB) | `hometwin/fusion/multilateration.py` |
| Poll loop, relocation inference, state | `hometwin/tracker.py` |
| HTTP endpoints, dashboard serving | `hometwin/api.py` |
| Dashboard markup / styling / behavior | `hometwin/static/ui.html`, `app.css`, `app.js` |
| Overlay payloads (map + camera) | `hometwin/overlay.py` |
| Cameras, ArUco, depth, capture modes | `hometwin/sensors/camera.py`, `detectors/`, `depth.py` |
| BLE tags, path loss, mesh anchors | `hometwin/sensors/ble.py` |
| Config schema and YAML loading | `hometwin/config.py`, `configs/*.yaml` |
| CLI subcommands | `hometwin/cli.py` |
| Plugin registration | `hometwin/registry.py` |
| Packaging / launchers | `packaging/{macos,windows,linux}/` |

Deeper topic docs in `docs/` (depth, presence-relocation, world-model,
calibration-anchors, security, performance, the platform app guides).

## Architecture

**Everything reduces to four observation shapes** (`observations.py`):
`PositionObservation`, `RangeObservation`, `BearingObservation`,
`AreaObservation`. New hardware emits one of these and never touches the fusion
engine. This is the seam that keeps sensors pluggable — preserve it.

**Plugins**: sensors, detectors, frame sources, depth estimators, and trainers
register by string name (`@register("sensor", "ble_mesh")`) and are named in
YAML. Adding hardware means writing a class and registering it; the core does
not change. Optional hardware deps are imported *inside* the concrete plugin,
never at module import, so a missing library degrades one plugin instead of
breaking startup.

**Fail-safe, not fail-stop.** A dead sensor, a lost camera, or a crashed step
must never halt the tracker: the poll loop logs the traceback and continues,
capture reopens itself, and consumers check validity flags rather than assume
data. Actuators hold last-known-good state.

**Pure-Python core.** `numpy`/`cv2` stay in vision and ML paths so the fusion
core, tests, and the `native`-equivalent test run install-free.

## Coding style

- Concise and modern; match the surrounding code.
- **No filler comments.** Comment only what a competent reader can't infer —
  hardware quirks, protocol constraints, and *why* a non-obvious approach was
  chosen. Docstrings explain intent, not mechanics.
- Short precise names: `temp` not `temperatureValue`, `pkt` not `packetBuffer`.
- Established acronyms bare: ISR, DMA, HAL, PWM, ADC, GPIO, MCU, OTA, RSSI, AoA.
- No defensive no-op error handling; fail fast at boundaries.
- Every behavior change gets a test that would fail without it.

## Security

- **Never hard-code credentials.** Wi-Fi from NVS or a git-ignored
  `data/config.json`; API tokens and bridge `auth_token` from env or file,
  never config literals.
- Keep the API on `127.0.0.1`; TLS terminates at a reverse proxy.
- Don't commit model binaries, captures, or `*.overrides.json`.
