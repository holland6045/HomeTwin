"""Passive 3D world model: the twin sketches its own space over time.

Every position the system already trusts — fused item fixes, presence
centroids, articulated tag positions, device estimates — deposits a little
evidence into a sparse voxel grid. No new sensing, no scheduled work, no
meaningful cost: a dict update per observation. Over days the apartment's
occupied/active space emerges as a point cloud (the Waymo look lives in
the dashboard's 3D tab and `render.pointcloud_svg`).

Hygiene follows the splat-merge ideology continuously instead of in
batches: evidence decays exponentially (half-life, default a week), so
stale geometry fades rather than accumulating; compaction drops voxels
below a weight floor (floaters/noise) and caps the total count by keeping
the heaviest. The model persists beside the tracker state and is served
at GET /pointcloud.

Long-term: the density prior is groundwork for surface guessing and
better single-camera depth priors — see docs/world-model.md.
"""

from __future__ import annotations

import gzip
import json
import math
import threading
from pathlib import Path

VOXEL_M = 0.12
HALF_LIFE_S = 7 * 24 * 3600.0
MIN_WEIGHT = 0.05
MAX_VOXELS = 250_000
COMPACT_EVERY = 2000  # observations between automatic decay/compact passes


class WorldModel:
    def __init__(
        self,
        voxel_m: float = VOXEL_M,
        half_life_s: float = HALF_LIFE_S,
        max_voxels: int = MAX_VOXELS,
    ):
        self.voxel_m = voxel_m
        self.half_life_s = half_life_s
        self.max_voxels = max_voxels
        self.voxels: dict[tuple[int, int, int], list[float]] = {}  # key -> [weight, last_ts]
        self.observations = 0
        self._since_compact = 0
        # tracker thread writes, API threads read/sort: guard the dict
        self._lock = threading.Lock()

    def _key(self, p) -> tuple[int, int, int]:
        return (
            math.floor(p[0] / self.voxel_m),
            math.floor(p[1] / self.voxel_m),
            math.floor(p[2] / self.voxel_m),
        )

    def add_point(self, p, ts: float, weight: float = 1.0) -> None:
        key = self._key(p)
        with self._lock:
            self._add_locked(key, ts, weight)

    def _add_locked(self, key, ts: float, weight: float) -> None:
        cell = self.voxels.get(key)
        if cell is None:
            self.voxels[key] = [weight, ts]
        else:
            # decay the stored weight to `ts`, then deposit
            cell[0] = cell[0] * 0.5 ** (max(ts - cell[1], 0.0) / self.half_life_s) + weight
            cell[1] = max(cell[1], ts)
        self.observations += 1
        self._since_compact += 1
        if self._since_compact >= COMPACT_EVERY:
            self._decay_locked(ts)

    def maintenance_now(self, wall: float) -> float:
        """Clock to decay against: wall time in live operation, the model's
        own newest timestamp when running on a synthetic/replay clock —
        otherwise one save would decay a sim-built model to dust."""
        with self._lock:
            newest = max((c[1] for c in self.voxels.values()), default=wall)
        return newest if wall - newest > 3600.0 else wall

    def decay_and_compact(self, now: float) -> int:
        """Fade everything to `now`, drop floaters, enforce the cap.
        Returns voxels removed."""
        with self._lock:
            return self._decay_locked(now)

    def _decay_locked(self, now: float) -> int:
        removed = 0
        for key in list(self.voxels):
            cell = self.voxels[key]
            cell[0] *= 0.5 ** (max(now - cell[1], 0.0) / self.half_life_s)
            cell[1] = now
            if cell[0] < MIN_WEIGHT:
                del self.voxels[key]
                removed += 1
        if len(self.voxels) > self.max_voxels:
            keep = sorted(self.voxels.items(), key=lambda kv: kv[1][0], reverse=True)
            for key, _ in keep[self.max_voxels:]:
                del self.voxels[key]
                removed += 1
        self._since_compact = 0
        return removed

    def point_cloud(self, max_points: int = 30_000) -> list[list[float]]:
        """[x, y, z, weight] at voxel centers, heaviest first."""
        with self._lock:
            items = [(k, w[0]) for k, w in self.voxels.items()]
        cells = sorted(items, key=lambda kv: kv[1], reverse=True)
        half = self.voxel_m / 2.0
        return [
            [
                round(k[0] * self.voxel_m + half, 3),
                round(k[1] * self.voxel_m + half, 3),
                round(k[2] * self.voxel_m + half, 3),
                round(w, 3),
            ]
            for k, w in cells[:max_points]
        ]

    def stats(self) -> dict:
        return {
            "voxels": len(self.voxels),
            "observations": self.observations,
            "voxel_m": self.voxel_m,
            "half_life_days": round(self.half_life_s / 86400.0, 2),
        }

    def save(self, path: str | Path) -> None:
        with self._lock:
            rows = [[*k, w, ts] for k, (w, ts) in self.voxels.items()]
        payload = {
            "voxel_m": self.voxel_m,
            "half_life_s": self.half_life_s,
            "observations": self.observations,
            "voxels": rows,
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp.replace(path)

    @classmethod
    def load(cls, path: str | Path) -> "WorldModel":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            return cls()
        model = cls(
            voxel_m=payload.get("voxel_m", VOXEL_M),
            half_life_s=payload.get("half_life_s", HALF_LIFE_S),
        )
        model.observations = payload.get("observations", 0)
        for ix, iy, iz, w, ts in payload.get("voxels", []):
            model.voxels[(int(ix), int(iy), int(iz))] = [float(w), float(ts)]
        return model
