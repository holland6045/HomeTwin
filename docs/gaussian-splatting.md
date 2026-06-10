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

- `world.splat_asset: scans/apartment.ply` in config, served at
  `GET /assets/splat`; `world.splat_transform` (position / rotation_deg /
  scale) aligns the scan to the world frame at render time.
- The dashboard's **3D tab** lazy-loads the `@mkkellogg/gaussian-splats-3d`
  WebGL renderer from CDN (vendor the modules locally for offline use) and
  renders **live tracker state inside the splat**: item markers with
  uncertainty spheres and labels, motion trails, BLE range rings, camera
  sight rays, the tomography heat map, presence, zone boxes, and camera
  poses — all sharing the dashboard's layer toggles. Without a configured
  splat the same tab renders the overlays over a ground grid. Rendering
  cost is paid by the viewing browser, never by the tracker host.
- **Automatic camera calibration** from the scan's COLMAP reconstruction:

  ```bash
  apartment-tracker calibrate-cameras \
      --colmap scans/colmap/sparse/0 \
      --pairs scans/refpoints.yaml \
      --images cam-kitchen.jpg cam-living-a.jpg
  ```

  parses `cameras.txt`/`images.txt`, aligns the reconstruction to the world
  frame from >= 2 reference points (2D similarity + height, gravity-aligned
  scans; `--up y` converts y-up exports), and prints ready-to-paste camera
  config (`position/yaw_deg/pitch_deg/hfov_deg`). It warns when alignment
  residual exceeds 0.15 m or a camera has > 5 deg roll (the pinhole model
  assumes level mounting). `refpoints.yaml` is a list of
  `{colmap: [x,y,z], world: [x,y,z]}` correspondences — e.g. two floor
  ArUco markers you can click in any COLMAP/splat viewer.

## Recommended workflow

1. Scan the apartment (Polycam/Luma/Scaniverse export, or
   `ns-train splatfacto` / OpenSplat on a video) → `.ply` + COLMAP model.
   Include a snapshot from each fixed camera in the image set.
2. Pick >= 2 reference points with known world coordinates, write
   `refpoints.yaml`.
3. `apartment-tracker calibrate-cameras ...` → paste camera poses into
   config; reuse the printed alignment as `world.splat_transform`.
4. Set `world.splat_asset`, open the 3D tab: live items inside your room.

## Scheduled rebuilds (weekly splat updates from a host PC)

The scan goes stale as the room changes; rebuilding it is a cron job on
whatever machine has the GPU — the tracker host is untouched:

1. `apartment-tracker capture-snapshots -c apartment.yaml -o images/` pulls
   one fresh frame from every configured camera (add walkthrough video
   frames for coverage).
2. Any splat pipeline (nerfstudio, OpenSplat, a phone-app export) trains
   the new model.
3. `apartment-tracker calibrate-cameras ...` re-derives camera poses from
   the same reconstruction — diff against your config to catch a bumped
   camera before it skews fusion.
4. `apartment-tracker update-splat new.ply [--transform '{...}']` pushes
   the scan to the running tracker over HTTP. The write is atomic
   (tmp + rename), no restart; open dashboards detect the version bump
   (asset mtime in `/overlay/map`) and reload the 3D scene automatically.

`scripts/rebuild_splat.sh` is the whole loop, ready for cron
(`0 4 * * 1`) or a systemd timer. There is also a `splat_rebuild` trainer
plugin wrapping the external pipeline command for setups that prefer the
trainer interface. Note: the upload endpoint is unauthenticated — keep the
API on localhost or behind an authenticated reverse proxy.

## Future work

- Splat-rendered synthetic views as training data for the ONNX detector
  (domain-matched backgrounds for your exact apartment).
- Click-to-measure reference-point picking inside the dashboard's 3D view.
