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

from hometwin import linalg as la
from hometwin.fusion.kalman import KalmanFilter3D
from hometwin.items import ItemRegistry
from hometwin.observations import (
    AreaObservation,
    BearingObservation,
    Observation,
    PositionObservation,
    RangeObservation,
)
from hometwin.world import World

GATE_MAHALANOBIS_SQ = 16.0  # ~4 sigma for anonymous-label association
ITEM_HEIGHT_PRIOR_M = 0.8  # items rest on floors/tables, not at ceiling height
ITEM_HEIGHT_SIGMA_M = 0.6
MIN_ANCHORS_FOR_INIT = 3
MAX_PENDING_BEFORE_INIT = 5
MIN_RAY_ANGLE_RAD = 0.035  # ~2 deg of viewpoint diversity before triangulating
# Coplanar (ceiling) anchors leave z nearly unobservable from ranges, so it
# random-walks under process noise. Tracks with no recent strong fix get a
# gentle height prior as a spring on that axis.
Z_PRIOR_AFTER_S = 5.0
Z_PRIOR_INTERVAL_S = 1.0
Z_PRIOR_SIGMA_M = 0.9
# adaptive trust via measurement self-consistency: consecutive fixes from the
# same sensor on a motion-compensated track scatter like 2x its claimed
# variance. A sensor claiming 5 cm while scattering 50 cm convicts itself —
# robust even when that sensor's overconfidence already corrupted the track
# (innovation-based metrics blame the honest sensor in that case). Trust
# inflates the claimed sigma, bounded so nothing is silenced or worshipped.
TRUST_MIN, TRUST_MAX = 0.7, 20.0
NIS_ALPHA = 0.05
SELF_CONSISTENCY_MAX_DT = 5.0

# people are tracked through the same estimator as items, in a separate
# registry. They move faster and matter less precisely than a set of keys,
# so the association gate is looser and a fresh detection more readily
# spawns a new occupant than hijacks an existing one.
PERSON_GATE_MAHALANOBIS_SQ = 25.0  # ~5 sigma
PERSON_HEIGHT_PRIOR_M = 1.2        # torso height for the single-ray fallback


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
    last_strong_update: float = 0.0  # last position/bearing/area fix
    last_z_prior: float = 0.0

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
        self.adaptive_trust = True
        self.sensor_nis: dict[str, float] = {}
        self._last_meas: dict[tuple[str, str], tuple[tuple, float]] = {}
        # occupants: same machinery, separate registry — never items
        self.people: dict[str, TrackState] = {}
        self._person_seq = 0

    def trust(self, sensor_id: str) -> float:
        nis = self.sensor_nis.get(sensor_id)
        if nis is None or not self.adaptive_trust:
            return 1.0
        return min(max(math.sqrt(nis), TRUST_MIN), TRUST_MAX)

    def _note_nis(self, sensor_id: str, nis_per_dof: float) -> None:
        prev = self.sensor_nis.get(sensor_id)
        self.sensor_nis[sensor_id] = (
            nis_per_dof if prev is None else (1 - NIS_ALPHA) * prev + NIS_ALPHA * nis_per_dof
        )

    def _note_self_consistency(self, obs: PositionObservation, item_id: str, track) -> None:
        key = (obs.sensor_id, item_id)
        prev = self._last_meas.get(key)
        self._last_meas[key] = (tuple(obs.position), obs.timestamp)
        if prev is None:
            return
        prev_pos, prev_ts = prev
        dt = obs.timestamp - prev_ts
        if not 0.0 < dt <= SELF_CONSISTENCY_MAX_DT:
            return
        v = track.filter.x[3:6]
        d2 = sum(
            (obs.position[i] - prev_pos[i] - v[i] * dt) ** 2 for i in range(3)
        )
        # E[d2] = 2 * 3 * R for an honest sensor on a well-tracked item
        self._note_nis(obs.sensor_id, d2 / (6.0 * obs.sigma_m**2))

    def _resolve(self, obs: Observation) -> str | None:
        if obs.item_id and self.items.get(obs.item_id):
            return obs.item_id
        if obs.item_id:
            mapped = self.items.resolve_tag(obs.item_id)
            if mapped:
                return mapped
        if obs.label:
            candidates = self.items.label_candidates(obs.label)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                return self._nearest_candidate(candidates, obs)
        return None

    def _nearest_candidate(self, candidates: list[str], obs: Observation) -> str | None:
        """Several identical-looking items share this label (storage boxes):
        attribute the sighting to the gated nearest existing track, or to no
        one — an anonymous look-alike must never seed or hijack a track."""
        best = None
        for item_id in candidates:
            track = self.tracks.get(item_id)
            if track is None:
                continue
            if isinstance(obs, PositionObservation):
                m = track.filter.mahalanobis_sq(obs.position, obs.sigma_m)
            elif isinstance(obs, BearingObservation):
                m = track.filter.mahalanobis_bearing_sq(obs.origin, obs.direction, obs.sigma_rad)
            else:
                continue
            if m <= GATE_MAHALANOBIS_SQ and (best is None or m < best[1]):
                best = (item_id, m)
        return best[0] if best else None

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
            sigma = obs.sigma_m
            if track is not None:
                track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
                if anonymous and track.filter.mahalanobis_sq(
                    obs.position, obs.sigma_m
                ) > GATE_MAHALANOBIS_SQ:
                    return None  # plausible look-alike elsewhere; don't hijack the track
                self._note_self_consistency(obs, item_id, track)
                sigma = obs.sigma_m * self.trust(obs.sensor_id)
            track = self._track_for(item_id, obs.position, obs.timestamp)
            track.filter.update_position(obs.position, sigma)
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
            if (
                obs.timestamp - track.last_strong_update > Z_PRIOR_AFTER_S
                and obs.timestamp - track.last_z_prior >= Z_PRIOR_INTERVAL_S
            ):
                track.filter.update_axis(2, ITEM_HEIGHT_PRIOR_M, Z_PRIOR_SIGMA_M)
                track.last_z_prior = obs.timestamp
        elif isinstance(obs, BearingObservation):
            track = self.tracks.get(item_id)
            if track is not None:
                track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
                if anonymous and track.filter.mahalanobis_bearing_sq(
                    obs.origin, obs.direction, obs.sigma_rad
                ) > GATE_MAHALANOBIS_SQ:
                    return None
                track.filter.update_bearing(
                    obs.origin, obs.direction, obs.sigma_rad * self.trust(obs.sensor_id)
                )
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
        if not isinstance(obs, RangeObservation):
            track.last_strong_update = obs.timestamp
        track.last_sensor = obs.sensor_id
        track.observation_count += 1
        track.contributors[obs.sensor_id] = track.contributors.get(obs.sensor_id, 0) + 1
        return item_id

    def ingest_presence(self, obs: Observation) -> str | None:
        """Track an occupant detection through the same Kalman/gating
        estimator as items, but in the separate `people` registry.

        People get smoothed position + velocity and multi-person
        association via the same Mahalanobis gate; they never enter item
        tracks, `/items`, persistence, or anonymous look-alike resolution.
        Returns the (auto-assigned) person id the detection was applied to.
        """
        if isinstance(obs, PositionObservation):
            seed = tuple(obs.position)
        elif isinstance(obs, BearingObservation):
            seed = ray_at_height(obs, PERSON_HEIGHT_PRIOR_M)
        else:
            return None
        # nearest gated occupant wins. Gating extrapolates each candidate to
        # the observation time (velocity + grown covariance) on a scratch
        # copy, so a person who moved between frames still associates instead
        # of spawning a fresh track every step.
        best = None
        for pid, tr in self.people.items():
            m = self._person_gate(tr, obs)
            if m <= PERSON_GATE_MAHALANOBIS_SQ and (best is None or m < best[1]):
                best = (pid, m)
        if best is None:
            self._person_seq += 1
            pid = f"person-{self._person_seq}"
            track = TrackState(pid, KalmanFilter3D(seed), last_update=obs.timestamp)
            self.people[pid] = track
        else:
            pid = best[0]
            track = self.people[pid]
            track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
        if isinstance(obs, PositionObservation):
            track.filter.update_position(obs.position, obs.sigma_m)
        else:
            track.filter.update_bearing(obs.origin, obs.direction, obs.sigma_rad)
        track.last_update = obs.timestamp
        track.last_sensor = obs.sensor_id
        track.observation_count += 1
        track.contributors[obs.sensor_id] = track.contributors.get(obs.sensor_id, 0) + 1
        return pid

    def _person_gate(self, track: "TrackState", obs: Observation) -> float:
        """Mahalanobis distance of obs to a person track, evaluated at the
        observation time. Predicts on a saved/restored copy so the live
        track is untouched (the matched one is predicted for real later)."""
        x0, P0 = list(track.filter.x), [row[:] for row in track.filter.P]
        track.filter.predict(max(obs.timestamp - track.last_update, 0.0))
        if isinstance(obs, PositionObservation):
            m = track.filter.mahalanobis_sq(obs.position, obs.sigma_m)
        else:
            m = track.filter.mahalanobis_bearing_sq(
                obs.origin, obs.direction, obs.sigma_rad)
        track.filter.x, track.filter.P = x0, P0
        return m

    def prune_people(self, now: float, timeout: float) -> list[str]:
        """Drop occupants unseen longer than `timeout` (people leave; unlike
        items they are not held indefinitely). Returns the dropped ids."""
        dead = [pid for pid, tr in self.people.items() if now - tr.last_update > timeout]
        for pid in dead:
            del self.people[pid]
        return dead

    def people_snapshot(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else time.time()
        out = []
        for pid, tr in self.people.items():
            v = tr.filter.x[3:6]
            out.append({
                "id": pid,
                "position": [round(c, 3) for c in tr.position],
                "velocity": [round(c, 3) for c in v],
                "speed_mps": round(math.sqrt(sum(c * c for c in v)), 2),
                "sigma_m": round(tr.sigma_m, 3),
                "zone": self.world.locate(tr.position),
                "age_s": round(now - tr.last_update, 1),
                "last_sensor": tr.last_sensor,
                "observations": tr.observation_count,
            })
        return out

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
                    spot=self.world.locate_spot(track.position),
                    sigma_m=round(track.sigma_m, 3),
                    age_s=round(age, 1),
                    last_sensor=track.last_sensor,
                    observations=track.observation_count,
                    sensors=dict(track.contributors),
                )
            out.append(entry)
        return out
