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

## Twin strips and calibration

A wide `--twin` strip carries the same marker ID at both ends. As an
*item* tag this is harmless (two sightings simply average in the filter),
but as a *calibration* anchor a twin strip surveyed as a single center
point has a failure mode: with one end occluded — the very situation twin
exists for — every sighting is offset by half the strip, biasing the pose
by `atan(half-separation / distance)` (a 200 mm strip at 2 m ≈ 2°, twice
the healthy threshold).

Survey **both marker centers** instead; the calibrator matches each
sighting against the nearest hypothesis (candidates sit degrees apart,
detection noise is tenths of a degree, so association is unambiguous):

```yaml
world:
  spots:
    - name: shelf-b3
      tag: "aruco:14"
      position: [4.0, 2.0, 0.9]            # spot center, for item naming
      tag_positions: [[4.0, 1.93, 0.9],    # left marker center
                      [4.0, 2.07, 0.9]]    # right marker center
  anchors:
    - {tag: "aruco:100", positions: [[0.0, 0.0, 1.0], [0.2, 0.0, 1.0]]}
```

With `tag_positions` set, one visible end calibrates exactly; without it,
prefer single-marker tags for anchors or accept the documented bias
(`tests/test_anchors.py` demonstrates both behaviors).

**Better still: distinct L/R codes.** `make-tag --layout wide --id 14
--twin-id 15` prints a different marker at each end. Each end is then an
ordinary single-position anchor — no hypothesis matching at all, exact
correspondence by ID, and when both ends are visible the camera gets two
independent, well-separated residuals per frame (better averaging and a
clean translation-drift signal, since rotation moves both residuals
together while translation splits them). Same occlusion resistance as
same-ID twin. Use same-ID twin only when you want one config line per
strip; use L/R for anchors:

```yaml
anchors:
  - {tag: "aruco:14", position: [4.0, 1.93, 0.9]}   # left end
  - {tag: "aruco:15", position: [4.0, 2.07, 0.9]}   # right end
```

## Shared reference across trackers

Two cameras watching the same block correct against the same physical
point, so their ray triangulations stay registered to each other and to
the world frame between scan-based recalibrations — multi-camera 3D fixes
do not slowly shear apart as individual cameras drift.

## Cost

One `world_to_pixel` + a few trig ops per anchor sighting, only on frames
where a detector already found the marker. Nothing on the fusion path.
