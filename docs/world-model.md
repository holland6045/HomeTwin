# Passive world model: the twin sketches its own space

A lower-priority, fully passive loop in the self-calibration ideology:
every position the system already trusts (fused item fixes weighted by
confidence, presence centroids, articulated door/drawer tag positions)
deposits evidence into a sparse voxel grid. No new sensing, no scheduled
jobs — a dict update per observation. Over days, the apartment's
occupied/active space emerges as a point cloud.

## Continuous hygiene instead of batch merging

The splat-merge idea ("merge two-iterations-old scans, clean floaters,
don't hoard history") is applied continuously:

- **Decay is the merge policy**: voxel evidence halves every week
  (configurable), so stale geometry fades into fresh observations
  instead of accumulating forever.
- **Floaters die at compaction**: voxels under a weight floor (single
  spurious fixes, noise) are dropped on every maintenance pass.
- **Bounded memory**: a hard voxel cap keeps the heaviest evidence.
- Maintenance runs piggyback on state saves and every ~2000 deposits;
  the model persists as gzip JSON next to the tracker state and
  survives restarts. (Synthetic/replay clocks are detected so a save
  never decays a sim-built model against wall time.)

For the photoreal layer, batch splat merging stays a GPU-host job — the
weekly `rebuild_splat.sh` retrain *is* the merge, since each rebuild
starts from fresh captures. The voxel model is the always-on complement.

## The Waymo view

- Dashboard 3D tab → **World model** layer: the cloud renders as
  `THREE.Points`, height-ramped deep-blue → cyan → green → amber →
  magenta, inside the splat or over the grid. Refreshes every 30 s.
- Headless: `GET /pointcloud` (voxel centers + weights, heaviest first)
  and `render.pointcloud_svg` for oblique-projected lidar-style stills.

## Long-term: the model improving capability

The density prior is groundwork, deliberately unexploited so far:
surface-height estimation per region (better single-camera depth priors
than the global 0.8 m item-height constant), free-space vs furniture
hints for BLE multilateration seeding, and drift detection when fresh
evidence consistently lands beside old mass. Each should arrive as its
own gated self-improvement loop.
