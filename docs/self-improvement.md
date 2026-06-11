# Self-improving location awareness

Every tracking path gets better with use — no labeling sessions, no
retrain buttons. Each loop is gated so it can only learn from evidence it
didn't produce itself, bounded so a bad day can't destroy a good
calibration, and inspectable via `/overlay/map -> learning`.

| Path | Loop | Proof (tests) |
|---|---|---|
| Camera pose | Hard anchors + soft references (settled item tags, closed movables) heal yaw/pitch drift online | bumped camera 1.5° → 0.05° |
| BLE ranging | Path-loss learner: camera-confirmed item positions turn raw RSSI into labeled samples; per-scanner (tx, exponent) refit online | factory model → environment truth ±0.25 exp; ranging error shrinks |
| Scanner positions | `device_tag` on the scanner: cameras triangulate the device, refining the anchor every range is measured from | 0.3 m survey error → 3 cm |
| Fusion weights | Adaptive trust: per-sensor measurement self-consistency vs claimed sigma; overconfident sensors get sigma inflated (×0.7..×20) | lying sensor demoted ×10, fused error 0.81 → 0.21 m |
| Tomography | Auto re-baseline: slow drift tracked (EMA), persistent attenuation (> 10 min) absorbed as moved furniture | ghost cleared, person still detected against new baseline |
| Detector | `detector_finetune` trainer hook (external GPU stack), dataset recorder | offline path |

## The gating discipline (why this doesn't explode)

Self-calibration's failure mode is feedback: a system that learns from its
own outputs can confidently drift anywhere. Every loop here breaks the
cycle:

- The **path-loss learner** only accepts samples while the item's track
  has a *fresh strong fix* (camera/bearing) — range-only tracks teach
  nothing (`test_no_learning_without_camera_confirmation`).
- **Soft references** require observations from sensors other than the
  camera being taught; **device tags** need >= 2 camera viewpoints
  (single rays have no depth) and corrections clamp at 1 m from config.
- **Adaptive trust** uses measurement *self-consistency* (consecutive
  fixes from the same sensor scatter like 2x its claimed variance) rather
  than innovation, because innovation-based metrics blame the honest
  sensor once an overconfident one owns the track. Bounded ×0.7..×20:
  nothing is silenced, nothing is worshipped.
- **Tomography re-baseline** only absorbs attenuation that persisted far
  longer than a person plausibly stands still, and per-link drift
  tracking never crosses the detection threshold.

## Inspect what the system has learned

```bash
curl -s localhost:8080/overlay/map | jq '.learning, .device_tags'
```

shows per-scanner learned path-loss parameters and refit counts,
per-sensor trust factors, and device-tag position residuals. Camera
calibration state lives in each camera's overlay entry
(`calibration.residual_deg`, `correction_yaw_deg`, `soft_residual_deg`).
