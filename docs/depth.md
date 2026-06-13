# Monocular depth (Depth Anything V2)

One camera that sees a fiducial of known distance can turn a relative
depth map into metric 3D for everything else in frame — replacing the
surface-plane / item-height-prior assumptions and sketching a dense
point cloud into the world model.

## Enable

```
hometwin get-model depth          # downloads the Apache-2.0 ONNX
```

Add a `depth:` block to a camera (alongside its detector):

```yaml
- type: camera
  id: cam-desk
  position: [1.5, 0.0, 2.2]
  yaw_deg: 90
  pitch_deg: 35
  surface_z: 0.0
  source: {type: opencv, device: 0, width: 1920, height: 1080, fourcc: MJPG}
  detector: {type: aruco, dictionary: DICT_4X4_250}
  depth:
    type: depth_anything
    model_path: models/depth.onnx
    interval_s: 1.0        # keyframe rate (a transformer pass is heavy)
    dense: true            # deposit a dense world-model cloud
    dense_stride: 24       # pixel stride for the cloud
    # scale: 3.2           # manual relative->metric override; omit to
                           # auto-fit from a visible surveyed anchor
```

## How scaling works

Depth Anything V2 outputs relative **inverse** depth (disparity): larger
= nearer. Metric distance ≈ `k / disparity`. `k` is fit online: whenever
a camera sees a tag whose world position is known (a surveyed `anchor`,
a tagged `spot`, the origin board), the system reads the disparity at
that tag's pixel and pairs it with the tag's true distance from the
camera. Three such samples and the scale is trusted; until then the
camera stays on its geometric fallback. A manual `scale:` skips this.

So: drop one anchor in view (you already do this for camera calibration)
and depth self-calibrates. `/overlay/map -> cameras[].depth` reports
`{ready, k, samples}`.

## What it produces

- **Metric item 3D off the plane**: a detection's distance comes from the
  depth at its pixel, back-projected along the sight ray — so an item on
  a shelf or in the air is placed correctly, not flattened onto the
  surface_z plane. Sigma grows with distance.
- **Dense world-model cloud**: a strided grid of the depth map is
  back-projected each keyframe and fed to the world model — the
  Waymo-style sketch from a single webcam. Occupants stay on the
  feet-on-floor path (more reliable for zones than torso depth).

## Performance

~0.8 s/frame for the small model on CPU; single-digit ms on a GPU.
Throttle with `interval_s`. For GPU on a Windows/Mac dev rig, install a
GPU ONNX Runtime — `pip install hometwin[vision,ml-dml]` (DirectML, any
vendor) or `[vision,ml-cuda]` (NVIDIA). The accel layer auto-selects the
provider; nothing else to configure.
