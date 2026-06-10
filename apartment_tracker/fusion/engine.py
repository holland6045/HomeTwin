"""Fusion engine: turns a stream of heterogeneous observations into one
best-estimate track per registered item.

Identity resolution order:
1. observation.item_id (sensor read the tag directly)
2. tag string in extras resolved through the ItemRegistry — done upstream
3. anonymous label ("keys") -> registry label mapping, then Mahalanobis
   gating against the existing track so a stranger's keys across the room
   don't teleport yours.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from apartment_tracker import linalg as la
from apartment_tracker.fusion.kalman import KalmanFilter3D
from apartment_tracker.items import ItemRegistry
from apartment_tracker.observations import (
    AreaObservation,
    BearingObservation,
    Observation,
    PositionObservation,
    RangeObservation,
)
from apartment_tracker.world import World

GATE_MAHALANOBIS_SQ = 16.0  # ~4 sigma for anonymous-label association
ITEM_HEIGHT_PRIOR_M = 0.8  # items rest on floors/tables, not at ceiling height
ITEM_HEIGHT_SIGMA_M = 0.6
MIN_ANCHORS_FOR_INIT = 3
MAX_PENDING_BEFORE_INIT = 5
MIN_RAY_ANGLE_RAD = 0.035  # ~2 deg of viewpoint diversity before triangulating


def triangulate_rays(rays: list[BearingObservation]) -> tuple[float, float, float] | None:
    """Least-squares closest point to a set of sight rays.

    Solves sum_i (I - d_i d_i^T)(p - o_i) = 0. Returns None when rays are
    too parallel to fix a point (single viewpoint, or cameras nearly in
    line with the target).
    """
    best_angle = 0.0
    for i, a in enumerate(rays):
        for b in rays[i + 1 :]:
            dot = sum(x * y for x, y in zip(a.direction, b.direction))
            best_angle = max(best_angle, math.acos(max(min(dot, 1.0), -1.0)))
    if best_angle < MIN_RAY_ANGLE_RAD:
        return None
    A = la.zeros(3, 3)
    b = [0.0, 0.0, 0.0]
    for ray in rays:
        d = ray.direction
        for r in range(3):
            for c in range(3):
                m = (1.0 if r == c else 0.0) - d[r] * d[c]
                A[r][c] += m
                b[r] += m * ray.origin[c]
    try:
        p = la.mat_vec(la.inverse(A), b)
    except ValueError:
        return None
    return (p[0], p[1], p[2])


def ray_at_height(obs: BearingObservation, z: float, fallback_t: float = 3.0) -> tuple:
    """Point along a single ray at item height — the one-camera fallback."""
    dz = obs.direction[2]
    t = (z - obs.origin[2]) / dz if abs(dz) > 1e-6 else -1.0
    if t <= 0:
        t = fallback_t
    return tuple(obs.origin[i] + obs.direction[i] * t for i in range(3))


def multilaterate(
    ranges: list[RangeObservation], z: float = ITEM_HEIGHT_PRIOR_M, step_m: float = 0.25
) -> tuple[float, float, float]:
    """Coarse grid search minimizing range residuals at item height.

    Used only to seed a new track; ceiling-mounted (coplanar) anchors make a
    naive EKF init land on the mirror solution above them, so we search at a
    physically plausible height instead.
    """
    pad = min(max(o.range_m for o in ranges), 12.0)
    xmin = min(o.anchor[0] for o in ranges) - pad
    xmax = max(o.anchor[0] for o in ranges) + pad
    ymin = min(o.anchor[1] for o in ranges) - pad
    ymax = max(o.anchor[1] for o in ranges) + pad
    best, best_cost = (xmin, ymin, z), float("inf")
    nx = int((xmax - xmin) / step_m) + 1
    ny = int((ymax - ymin) / step_m) + 1
    for iy in range(ny):
        y = ymin + iy * step_m
        for ix in range(nx):
            x = xmin + ix * step_m
            cost = 0.0
            for o in ranges:
                dx, dy, dz = x - o.anchor[0], y - o.anchor[1], z - o.anchor[2]
                r = (dx * dx + dy * dy + dz * dz) ** 0.5
                cost += ((r - o.range_m) / max(o.sigma_m, 1e-3)) ** 2
            if cost < best_cost:
                best, best_cost = (x, y, z), cost
    return best


@dataclass
class TrackState:
    item_id: str
    filter: KalmanFilter3D
    last_update: float
    last_sensor: str = ""
    observation_count: int = 0
    contributors: dict[str, int] = field(default_factory=dict)

    @property
    def position(self) -> tuple[float, float, float]:
        return self.filter.position

    @property
    def sigma_m(self) -> float:
        return self.filter.position_sigma


class FusionEngine:
    def __init__(self, items: ItemRegistry, world: World, stale_after_s: float = 300.0):
        self.items = items
        self.world = world
        self.stale_after_s = stale_after_s
        self.tracks: dict[str, TrackState] = {}
        # range/bearing observations buffered per item until a track can be seeded
        self._pending: dict[str, dict[tuple, RangeObservation]] = {}
        self._pending_count: dict[str, int] = {}
        self._pending_rays: dict[str, dict[str, BearingObservation]] = {}
        self._pending_ray_count: dict[str, int] = {}

    def _resolve(self, obs: Observation) -> str | None:
        if obs.item_id and self.items.get(obs.item_id):
            return obs.item_id
        if obs.item_id:
            mapped = self.items.resolve_tag(obs.item_id)
            if mapped:
                return mapped
        if obs.label:
            return self.items.resolve_label(obs.label)
        return None

    def _track_for(self, item_id: str, seed: tuple[float, float, float], ts: float) -> TrackState:
        track = self.tracks.get(item_id)
        if track is None:
            track = TrackState(item_id, KalmanFilter3D(seed), last_update=ts)
            self.tracks[item_id] = track
        return track

    def ingest(self, obs: Observation) -> str | None:
        """Fuse one observation. Returns the item_id it was applied to, if any."""
        item_id = self._resolve(obs)
        if item_id is None:
            return None

        anonymous = not obs.item_id

        if isinstance(obs, PositionObservation):
            track = self.tracks.get(item_id)
            if track is not None:
                track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
                if anonymous and track.filter.mahalanobis_sq(
                    obs.position, obs.sigma_m
                ) > GATE_MAHALANOBIS_SQ:
                    return None  # plausible look-alike elsewhere; don't hijack the track
            track = self._track_for(item_id, obs.position, obs.timestamp)
            track.filter.update_position(obs.position, obs.sigma_m)
        elif isinstance(obs, RangeObservation):
            track = self.tracks.get(item_id)
            if track is None:
                pend = self._pending.setdefault(item_id, {})
                pend[tuple(obs.anchor)] = obs
                self._pending_count[item_id] = self._pending_count.get(item_id, 0) + 1
                if (
                    len(pend) < MIN_ANCHORS_FOR_INIT
                    and self._pending_count[item_id] < MAX_PENDING_BEFORE_INIT
                ):
                    return item_id  # buffered until the track can be seeded
                seed = multilaterate(list(pend.values()))
                track = self._track_for(item_id, seed, obs.timestamp)
                track.filter.P[0][0] = track.filter.P[1][1] = 1.0
                track.filter.P[2][2] = ITEM_HEIGHT_SIGMA_M**2
                for buffered in pend.values():
                    track.filter.update_range(buffered.anchor, buffered.range_m, buffered.sigma_m)
                del self._pending[item_id]
                del self._pending_count[item_id]
            else:
                track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
                track.filter.update_range(obs.anchor, obs.range_m, obs.sigma_m)
        elif isinstance(obs, BearingObservation):
            track = self.tracks.get(item_id)
            if track is not None:
                track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
                if anonymous and track.filter.mahalanobis_bearing_sq(
                    obs.origin, obs.direction, obs.sigma_rad
                ) > GATE_MAHALANOBIS_SQ:
                    return None
                track.filter.update_bearing(obs.origin, obs.direction, obs.sigma_rad)
            else:
                pend = self._pending_rays.setdefault(item_id, {})
                pend[obs.sensor_id] = obs
                self._pending_ray_count[item_id] = self._pending_ray_count.get(item_id, 0) + 1
                seed = triangulate_rays(list(pend.values())) if len(pend) >= 2 else None
                if seed is None:
                    if self._pending_ray_count[item_id] < MAX_PENDING_BEFORE_INIT:
                        return item_id  # buffered until a second viewpoint arrives
                    # single-viewpoint fallback: depth from the item-height prior
                    seed = ray_at_height(obs, ITEM_HEIGHT_PRIOR_M)
                track = self._track_for(item_id, seed, obs.timestamp)
                track.filter.P[0][0] = track.filter.P[1][1] = 1.0
                track.filter.P[2][2] = ITEM_HEIGHT_SIGMA_M**2
                for buffered in pend.values():
                    track.filter.update_bearing(
                        buffered.origin, buffered.direction, buffered.sigma_rad
                    )
                del self._pending_rays[item_id]
                del self._pending_ray_count[item_id]
        elif isinstance(obs, AreaObservation):
            track = self.tracks.get(item_id)
            if track is not None:
                track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
            track = self._track_for(item_id, obs.centroid, obs.timestamp)
            track.filter.update_position(obs.centroid, obs.sigma_m)
        else:
            return None

        track.last_update = obs.timestamp
        track.last_sensor = obs.sensor_id
        track.observation_count += 1
        track.contributors[obs.sensor_id] = track.contributors.get(obs.sensor_id, 0) + 1
        return item_id

    def dump_state(self) -> list[dict]:
        """Serializable track state for persistence across restarts."""
        return [
            {
                "item_id": t.item_id,
                "x": list(t.filter.x),
                "P": [row[:] for row in t.filter.P],
                "last_update": t.last_update,
                "last_sensor": t.last_sensor,
                "observation_count": t.observation_count,
                "contributors": dict(t.contributors),
            }
            for t in self.tracks.values()
        ]

    def restore_state(self, entries: list[dict]) -> int:
        """Recreate tracks from dump_state output. Returns tracks restored.

        Entries for items no longer in the registry are skipped; original
        timestamps are kept so age/staleness stay truthful.
        """
        restored = 0
        for e in entries:
            if self.items.get(e["item_id"]) is None or e["item_id"] in self.tracks:
                continue
            kf = KalmanFilter3D(tuple(e["x"][:3]))
            kf.x = [float(v) for v in e["x"]]
            kf.P = [[float(v) for v in row] for row in e["P"]]
            self.tracks[e["item_id"]] = TrackState(
                item_id=e["item_id"],
                filter=kf,
                last_update=float(e["last_update"]),
                last_sensor=e.get("last_sensor", ""),
                observation_count=int(e.get("observation_count", 0)),
                contributors=dict(e.get("contributors", {})),
            )
            restored += 1
        return restored

    def snapshot(self, now: float | None = None) -> list[dict]:
        """Current best estimate per item, for the API/CLI layers."""
        now = now if now is not None else time.time()
        out = []
        for item in self.items.all():
            track = self.tracks.get(item.item_id)
            entry: dict = {"item_id": item.item_id, "name": item.name}
            if track is None:
                entry.update(status="never_seen")
            else:
                age = now - track.last_update
                entry.update(
                    status="stale" if age > self.stale_after_s else "tracked",
                    position=[round(v, 3) for v in track.position],
                    zone=self.world.locate(track.position),
                    sigma_m=round(track.sigma_m, 3),
                    age_s=round(age, 1),
                    last_sensor=track.last_sensor,
                    observations=track.observation_count,
                    sensors=dict(track.contributors),
                )
            out.append(entry)
        return out
