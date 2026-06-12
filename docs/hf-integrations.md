# Hugging Face survey: candidate model integrations

Surveyed 2026-06. The detector plugin registry means each of these is a
wrapper + config entry, never a core change. ONNX-first to ride the
existing accel layer (TensorRT/CUDA/CoreML auto-selection).

## Priority 1 — RT-DETRv2: better closed-set detector, clean license

**Status: landed, turnkey.** Plugin `rtdetr` (`hometwin/detectors/rtdetr.py`).
`hometwin get-model rtdetr` downloads the pre-exported ONNX
(`onnx-community/rtdetr_r18vd`, checksummed; fp16/int8 variants
available) — no torch/optimum toolchain. Then:
`detector: {type: rtdetr, model_path: models/rtdetr.onnx, interval_s: 0.5}`.
Person detections route into motion zones via `tracker.presence_labels`
(default `[person]`) — synthetic PIRs work from a webcam alone.
Validated end-to-end on a real low-light webcam frame (person @ 0.93,
0.22 s/frame on CPU). `scripts/export_rtdetr.sh` remains the path to
the v2 weights if the accuracy bump is ever wanted.

- `PekingU/rtdetr_v2_r50vd` / `r18vd` (Apache-2.0, COCO-trained,
  450K/92K downloads). NMS-free transformer detector, exports to ONNX
  via transformers.
- Why: our `onnx` detector assumes YOLO output layout, and the obvious
  YOLO weights (ultralytics v8/11, `Bingsu/yolo-world-mirror`) are
  **AGPL-3.0** — fine for personal use, a wall for anything more.
  RT-DETRv2 gives COCO classes (person, cell phone, backpack, handbag,
  laptop...) under Apache.
- Integration: output-layout adapter in the `onnx` detector (boxes +
  class logits instead of YOLO rows). Small session.
- Unlocks immediately: **camera person-detections as motion-zone
  evidence** (synthetic PIRs work without the RF mesh) and tagless
  tracking of COCO-class items (phone, laptop, backpack).

## Priority 2 — Depth Anything V2-small: single-camera depth prior

- `onnx-community/depth-anything-v2-small` (Apache-2.0; note: base/large
  are CC-BY-NC). V3-small also up (Apache), newer and worth A/B.
- Why: our single-camera depth comes from the surface-plane assumption
  or the 0.8 m item-height prior. Relative depth per frame, scaled to
  metric against things we already know (origin-board distance, anchor
  distances, known surface z), replaces both — and can deposit *dense*
  world-model points: the Waymo cloud from one webcam instead of sparse
  fix trails.
- Integration: optional depth module on the camera path, keyframe rate
  (not per frame); scale calibration via board-check. Medium session;
  GPU helps but small runs CPU-tolerably at low rate.

## Priority 3 — Zero-shot detection: tagless items without training

- `google/owlv2-base-patch16-ensemble` (Apache-2.0, 924K downloads) +
  ready ONNX: `onnx-community/owlv2-base-patch16-ONNX`.
- `IDEA-Research/grounding-dino-tiny` (Apache-2.0, 596K) +
  `onnx-community/grounding-dino-tiny-ONNX` — better text grounding,
  heavier.
- Why: text-prompted detection ("set of keys", "brown leather wallet")
  makes Block 8 partly unnecessary — tagless items day one, no dataset,
  no finetune. Anonymous-label fusion (gated nearest-track) already
  handles the identity side.
- Cost: ~1–2 fps CPU for base models — run on keyframes/low rate, let
  tags carry the fast loop. New detector plugin (text+image encoder
  wrapper, different pre/post than YOLO). Medium session.
- License caution: YOLO-World (fast open-vocab alternative) is
  AGPL/GPL — skip.

## Priority 4 — Floorplan understanding: auto-zones from the import

- `mudasir13cs/qwen25-vl-3b-floorplan-grpo` (Apache-2.0 LoRA on
  Qwen2.5-VL-3B): floorplan image → structured JSON vectorization
  (CubiCasa5k-trained). Could turn `hometwin floorplan` output into
  auto-generated `world.zones` — rooms named and bounded without
  drawing anything.
- `Patnev71/segformer-b0-finetuned-floorplan`: lightweight wall
  segmentation — an upgrade path for our threshold-based cleanup.
- Dataset: `Claudio9701/cubicasa5k` if we ever train our own.
- GPU-host job (3B VLM); exploratory session after Block 1 feedback.

## Watchlist (no action now)

- `apple/MobileCLIP2-S2` ONNX ports: embedding search over detection
  crops ("find my red mug") and appearance re-ID for identical boxes —
  but the `apple-amlr` license needs reading before integration.
- `Voxel51/FloorPlanCAD` (CC-BY-SA): floorplan symbol detection if P4
  needs more structure.

## Suggested sequencing

P1 landed (small, immediate motion-zone payoff);
P2 after Block 1 confirms webcam fps headroom; P3 when tagless items
become a daily-use want; P4 exploratory alongside Block 5's GPU work.
