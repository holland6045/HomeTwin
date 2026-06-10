# Gaussian splatting: viability assessment

Question: should the apartment tracker use 3D Gaussian splatting (3DGS),
and for what?

## TL;DR

| Use | Verdict |
|---|---|
| Photoreal apartment digital twin as the dashboard's 3D backdrop | **Viable now** — offline scan, browser-side rendering, zero new Python deps. Implemented as an optional asset hook. |
| Camera extrinsics calibration (the real win) | **Viable and valuable** — the SfM step of any splat pipeline produces exactly the camera poses our config needs hand-measured today. |
| Live perception (tracking items by re-splatting) | **Not viable / wrong tool** — items are found by detectors + tags; splats are static scene representations. Dynamic (4D) splatting is research-grade and GPU-bound. |

## What 3DGS is

A scene is represented as millions of anisotropic 3D Gaussians (position,
covariance, opacity, view-dependent color) optimized from posed photos, and
rendered by sorted rasterization — photoreal novel views at real-time rates.
Producing one requires: (1) a video/photo scan of the room, (2) SfM
(COLMAP) to recover camera poses, (3) GPU training (~minutes on a desktop
GPU with gsplat/OpenSplat/nerfstudio, or a phone app / cloud service that
outputs `.ply`/`.splat` directly).

## Why it fits this project

1. **Calibration.** Our weakest deployment step is hand-measuring each
   camera's position/yaw/pitch. Run the splat scan with the fixed cameras'
   own snapshots included in the image set: SfM localizes them in the same
   coordinate frame as the scan. Align the scan to the world frame once
   (3-point alignment), and every camera pose drops out for free — and any
   new camera is calibrated by registering one snapshot against the model.
2. **Spatial authoring.** Zone boxes (`world.zones`) are easier to author
   against a photoreal 3D model than against a tape measure.
3. **Presentation.** "Your keys are *here*" rendered inside a photoreal
   room beats a 2D rectangle map.

## Why the live loop should NOT use it

- Splats are frozen scene geometry; a wallet that moved is exactly what the
  splat does not show. Live state belongs to detectors/tags/fusion.
- Training and even rendering large splats need a GPU; the tracker core
  deliberately runs on anything.
- 4D/dynamic splatting exists in the literature but is nowhere near
  edge-deployable; revisit in a couple of years.

## What is implemented today

- `world.splat_asset: scans/apartment.ply` in config.
- The API serves it at `GET /assets/splat`.
- The dashboard shows a **3D scan** tab when configured, lazy-loading the
  `@mkkellogg/gaussian-splats-3d` WebGL renderer from CDN (vendor the module
  locally for offline use). Rendering cost is paid by the viewing browser,
  never by the tracker host.

## Recommended workflow

1. Scan the apartment (Polycam/Luma/Scaniverse export, or
   `ns-train splatfacto` / OpenSplat on a video) → `.ply`.
2. Align the model to the world frame (same origin/axes as `world.zones`).
3. Set `world.splat_asset`, open the 3D tab.
4. (Next step, not yet automated) feed fixed-camera snapshots through the
   same SfM run and copy the recovered poses into each camera's config.

## Future work

- Automated camera-pose import from a COLMAP reconstruction
  (`images.txt` → camera `position/yaw_deg/pitch_deg`).
- Item markers rendered inside the 3D view at fused world coordinates
  (the viewer already has the world-frame scene; this is a small JS step).
- Splat-rendered synthetic views as training data for the ONNX detector
  (domain-matched backgrounds for your exact apartment).
