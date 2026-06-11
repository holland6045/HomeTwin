# Blocky calibration anchors

Printed ArUco blocks at surveyed world positions, shared as a fixed point
of reference by every visual tracker that can see one. **Optional for
function** — every camera runs on its configured pose alone; anchors add
drift detection and self-healing on top.

## Why

Camera extrinsics are measured once (tape measure or
`calibrate-cameras` from a splat scan) and then trusted forever. Reality
bumps cameras. A 1.5° yaw knock moves a 3 m-away item estimate ~8 cm and
nothing in the system would know. An anchor is ground truth that is always
in frame: cheap to print, survey once, works forever.

## Setup

1. Generate and print a target (IDs 100+ keep anchors out of the item-tag
   range):

   ```bash
   apartment-tracker make-anchor --id 100 --pixels 800 -o anchor-100.png
   ```

2. Mount it flat and rigid where a camera (ideally several) can see it.
   Measure its center in world coordinates — or click it in the splat
   viewer and reuse it as a `calibrate-cameras` reference point, which is
   the same physical block doing double duty.

3. Configure:

   ```yaml
   world:
     anchors:
       - {tag: "aruco:100", position: [6.9, 1.5, 0.9]}
   sensors:
     - type: camera
       id: cam-kitchen
       anchor_correct: true   # optional; default is report-only
       ...
   ```

Anchors attach to every camera automatically; a camera that never sees one
is simply unaffected.

## What happens at runtime

Anchor detections are consumed as calibration input — they are never item
observations. For each sighting the camera compares the observed sight ray
with the known direction to the block:

- **Report mode (default):** the angular residual (EMA-smoothed) is
  exposed in the camera's overlay layer and the API; the dashboard renders
  a drifted camera red. You decide when to re-aim or re-calibrate.
- **Correct mode (`anchor_correct: true`):** the yaw/pitch residual is
  folded back into the live camera geometry, smoothed, and **clamped to
  ±10° from the configured pose** — an occluded or misdetected marker
  cannot walk a camera across the room. Rotation drift (the dominant bump
  mode) self-heals within seconds.

Translation drift cannot be separated from rotation using sparse anchors,
so a large *post-correction* residual marks the camera `healthy: false`:
that is the signal to re-run the splat calibration rather than trust the
correction.

The bundled simulation proves the loop: the kitchen camera's configured yaw
is deliberately 1.5° off its true mounting; the counter anchor pulls it
back and the keys stay correctly placed (`tests/test_anchors.py`).

## Shared reference across trackers

Two cameras watching the same block correct against the same physical
point, so their ray triangulations stay registered to each other and to
the world frame between scan-based recalibrations — multi-camera 3D fixes
do not slowly shear apart as individual cameras drift.

## Cost

One `world_to_pixel` + a few trig ops per anchor sighting, only on frames
where a detector already found the marker. Nothing on the fusion path.
