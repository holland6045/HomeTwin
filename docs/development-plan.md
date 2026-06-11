# Development plan: iterate between real-world test blocks

Structure: each block is one vibe-coding session's worth of software work
followed by a hardware test **you** run in the apartment. Every block ends
with a test protocol, pass criteria, and the exact artifacts to bring back
to the next session — that feedback decides what the next block actually
needs. Blocks are ordered so each one's hardware builds on the last, but
B4–B7 can be reordered freely based on what hardware arrives first.

Update loop throughout: I push to the channel branch → you relaunch
HomeTwin.app (auto-updates) → run the protocol → bring back artifacts.

**Bring-back artifacts, every block** (cheap to collect, diagnose almost
everything):
- `curl -s localhost:8080/overlay/map > overlay.json`
- `.../venv/bin/hometwin snapshot-map --url http://localhost:8080 -o map.svg`
- the tail of `~/Library/Logs/HomeTwin/hometwin.log`
- your subjective notes: what felt wrong, slow, or surprising.

---

## Block 1 — First light: Mac + webcam  *(software done — your move)*

Hardware: your Mac, its webcam, a printer.

Protocol:
1. Build/copy `HomeTwin.app` (docs/macos-app.md), right-click → Open.
2. Print `make-board` (the origin board — lay it flat, it IS the world
   origin) and `make-anchor --id 7` (keys tag).
3. `webcam-test` → confirm detections and note fps.
4. `webcam-setup --board`; paste the printed camera block into the app
   config; relaunch. (No coordinates, no tape measure.)
5. Optional: `hometwin floorplan <your listing URL> --width-m <unit
   width>` and add the snippet — the map gets your actual floorplan as
   its background; world (0,0) is the plan's bottom-left, so put the
   board there.
6. Tape tag 7 to your keys. Move them around the desk. Watch the
   dashboard; `hometwin where keys`.

Pass: keys tracked live; reported positions match a ruler to ~±5 cm near
the board; dashboard updates ~1 s; survives app relaunch (persistence).

Bring back: artifacts + measured-vs-reported positions at 3 desk spots +
webcam-test fps + `reprojection_error_px` and `markers_used` from
webcam-setup --board.

## Block 2 — Trust the picture: anchors, spots, drift  *(software done)*

Hardware: same, plus 2–3 more printed tags.

Protocol:
1. Add marker 100 to config as an anchor; set `anchor_correct: true`.
2. Define 2 spots (e.g. "keys-tray", "charging-pad") with printed wide
   tags on their edges; add a `maybe_in`-style drawer if you have one in
   view (movable config).
3. Deliberately nudge the camera ~2° and watch
   `calibration.correction_yaw_deg` heal it; then nudge it hard (>10°)
   and confirm the camera goes red (unhealthy) instead of guessing.
4. Keys into the tray → `where keys` should answer "keys-tray".

Pass: drift self-heals for small bumps, flags for big ones; spot-level
answers correct.

Bring back: artifacts + the calibration block of the camera overlay
before/after each nudge.

## Block 3 — Second viewpoint: multi-camera 3D  *(software done)*

Hardware: any second camera — old phone running an IP-webcam app
(MJPEG URL), ESP32-CAM, or a USB webcam.

Protocol:
1. Add the second camera (`source: {type: opencv, device: "http://...mjpeg"}`),
   pose via `webcam-setup` against the same flat marker.
2. Switch both to `mode: ray` where views overlap.
3. Walk the keys through mid-air across the room; watch trails + camera
   rays converge on the dashboard.

Pass: mid-air tracking (no surface assumption) within ~10 cm in the
overlap zone; sigma visibly tighter than single-camera.

Bring back: artifacts + a trail screenshot of the same walk with one
camera disabled vs both (the comparison drives any fusion tuning next
session).

Likely next-session software from B1–B3 feedback: lens-distortion
handling (cheap webcams), exposure/fps quirks, detector tuning at range.

## Block 4 — Radios: ESP32 BLE scanners + self-learning  *(software done)*

Hardware: 1–3 ESP32 dev boards, any BLE beacon/tag on the keys (or a
spare phone advertising).

Protocol:
1. Flash `firmware/esp32-ble-scanner` (set `BRIDGE_TOKEN`, anchor
   position per board); add `network_bridge` with `auth_token` to config.
   On connect the node announces hello and the bridge auto-benchmarks it:
   check `/overlay/map -> devices` for RTT/loss and the per-MAC RSSI
   envelope — a bad antenna or flaky Wi-Fi shows up here before it
   pollutes tracking.
2. Stick a printed `device_tag` on each scanner; let the cameras refine
   scanner positions (watch `device_tags` residuals).
3. Carry the keys (BLE + visual tag) around for 15 minutes in camera
   view — the path-loss learner harvests samples; check
   `learning.path_loss` refits and exponent drift.
4. Then hide the keys in a camera-blind spot: BLE alone should place them
   in the right zone.

Pass: blind-spot zone resolution correct; learned exponent stabilizes;
ranging visibly better after the 15-minute walk than before (compare
`rings` tightness on the map).

Bring back: artifacts + `learning` JSON before/after the walk + the
`devices` benchmark block per node + which zones BLE got wrong.

Likely software next: per-room path-loss segmentation if one global
exponent per scanner proves too coarse; scanner placement advice.

## Block 5 — The photoreal twin: splat scan  *(software done)*

Hardware: your phone (Polycam/Luma/Scaniverse), optionally a GPU PC.

Protocol:
1. Scan the room (include one snapshot from each fixed camera in the
   image set); export `.ply` (+ COLMAP model if the app provides it).
2. `calibrate-cameras` against two reference points (your printed
   anchors!) → compare derived poses with the webcam-setup ones.
3. Set `world.splat_asset` (+ transform); open the 3D tab: live items
   inside your actual room.
4. If GPU PC available: dry-run `scripts/rebuild_splat.sh` end-to-end and
   `update-splat` push.

Pass: items render in the right place inside the scan; calibrate-cameras
agrees with PnP poses within ~0.1 m / 2°. Toggle the World model layer:
by now the passive voxel cloud should sketch your active surfaces inside
the splat — bring a screenshot of cloud-vs-splat agreement (drift between
them is a calibration smell).

Bring back: artifacts + the alignment residual + a 3D-tab screen
recording (this is the first "wow" checkpoint — worth recording).

## Block 6 — Drawers, doors, stowed items  *(software done)*

Hardware: printed wide/twin tags on 1 drawer + 1 door.

Protocol: configure both movables; cycle them; put the keys in the open
drawer, close it, confirm `where keys` → "likely inside …"; reopen and
remove, confirm the hint clears; check open/close events in the feed.

Pass: openness tracks smoothly, events fire once per cycle (no chatter),
stowed-inference correct in both directions.

Bring back: artifacts + event log excerpt + any false open/close flaps.

## Block 7 — Presence mesh (stretch)  *(software done, hardware new)*

Hardware: 4–8 ESP32s as an RF link mesh (or defer — tomography is the
most hardware-hungry block and everything else works without it).

Protocol: mount nodes around one room; node firmware forwards per-link
RSS over the bridge (`area`/link messages); calibrate empty-room; walk
around; verify the heat blob follows you; move a chair and confirm
auto-re-baseline absorbs it within ~10 min.

Bring back: artifacts + heat-map SVGs of you standing at 4 known spots.

## Block 8 — Tagless items: detector finetune  *(pipeline exists, model work new)*

After B1–B3 have produced weeks of frames: record a dataset
(`DatasetRecorder` + camera crops), fine-tune YOLO → ONNX
(`detector_finetune`), and let anonymous "wallet"-class detections refine
tagged tracks / track the genuinely untaggable. First block where the
session is mostly model work rather than systems work.

---

## Standing backlog (pull in when feedback demands)

- Lens distortion coefficients in `CameraGeometry` (likely surfaces in B1/B3).
- Event/zone hysteresis if BLE noise flaps zone events (B4).
- Menu-bar status for the Mac app; LaunchAgent autostart (whenever the
  app loop annoys you).
- Vendored 3D renderer modules for offline dashboards (B5).
- UWB ranging plugin if BLE accuracy disappoints (B4 verdict).
