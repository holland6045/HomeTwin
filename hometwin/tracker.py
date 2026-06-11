"""Tracker orchestrator: polls every sensor, feeds the fusion engine, and
keeps the queryable state the API/CLI expose.

One sensor failing (raising in poll) is logged and skipped — never fatal.
Presence-only observations (no item resolution) are kept separately so
occupancy is queryable even though it fuses into no item track.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque

from hometwin.config import AppConfig
from hometwin.fusion import FusionEngine
from hometwin.observations import (
    AreaObservation,
    BearingObservation,
    RangeObservation,
)
from hometwin.store import StateStore

log = logging.getLogger("hometwin")

EVENT_HISTORY = 500
TRAIL_LENGTH = 200
TRAIL_MIN_STEP_M = 0.15
# soft-reference qualification: a tag teaches a camera only when its fused
# estimate is tight, stationary, and substantially owed to OTHER sensors
SOFT_REF_MAX_SIGMA_M = 0.2
SOFT_REF_MAX_SPEED = 0.05
SOFT_REF_MIN_OTHER_OBS = 10


class Tracker:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.engine = FusionEngine(cfg.items, cfg.world, stale_after_s=cfg.stale_after_s)
        self.sensors = cfg.sensors
        self.presence: dict | None = None
        self.events: deque[dict] = deque(maxlen=EVENT_HISTORY)
        self.last_ranges: dict[tuple[str, str], dict] = {}  # (sensor, item) -> last range
        self.last_bearings: dict[tuple[str, str], dict] = {}  # (sensor, item) -> last ray
        self.trails: dict[str, deque] = {}  # item -> recent path points
        self._zones: dict[str, str | None] = {}
        self._stop = threading.Event()
        # RLock: overlay/snapshot helpers nest under step()'s critical section
        self._lock = threading.RLock()
        self.store = StateStore(cfg.state_path) if cfg.state_path else None
        if self.store:
            restored = self.engine.restore_state(self.store.load())
            if restored:
                log.info("restored %d track(s) from %s", restored, cfg.state_path)
            # restored locations are the zone baseline, not "arrival" events
            for t in self.engine.tracks.values():
                self._zones[t.item_id] = (
                    cfg.world.locate(t.position),
                    cfg.world.locate_spot(t.position),
                )

    def save_state(self) -> None:
        if self.store:
            with self._lock:
                state = self.engine.dump_state()
            self.store.save(state)

    def start_sensors(self) -> None:
        for s in self.sensors:
            s.start()

    def stop_sensors(self) -> None:
        for s in self.sensors:
            try:
                s.stop()
            except Exception:
                log.exception("sensor %s failed to stop", s.sensor_id)

    def step(self) -> int:
        """Poll all sensors once and fuse. Returns observations processed."""
        count = 0
        touched: set[str] = set()
        for sensor in self.sensors:
            try:
                batch = sensor.poll()
            except Exception:
                log.exception("sensor %s poll failed", sensor.sensor_id)
                continue
            for obs in batch:
                count += 1
                # remote cameras report movable tags as bearings: route to
                # the door/drawer state estimator, not the fusion engine
                if isinstance(obs, BearingObservation) and obs.item_id:
                    if self.cfg.movables is not None and self.cfg.movables.observe_ray(
                        obs.item_id, obs.origin, obs.direction, obs.timestamp
                    ):
                        continue
                    if self.cfg.device_tags is not None and self.cfg.device_tags.observe_ray(
                        obs.item_id, obs.origin, obs.direction, obs.timestamp
                    ):
                        continue
                with self._lock:
                    applied = self.engine.ingest(obs)
                if applied:
                    touched.add(applied)
                    if isinstance(obs, RangeObservation):
                        self.last_ranges[(obs.sensor_id, applied)] = {
                            "anchor": tuple(obs.anchor),
                            "range_m": obs.range_m,
                            "sigma_m": obs.sigma_m,
                            "timestamp": obs.timestamp,
                        }
                    elif isinstance(obs, BearingObservation):
                        self.last_bearings[(obs.sensor_id, applied)] = {
                            "origin": tuple(obs.origin),
                            "direction": tuple(obs.direction),
                            "timestamp": obs.timestamp,
                        }
                elif isinstance(obs, AreaObservation):
                    self.presence = {
                        "sensor_id": obs.sensor_id,
                        "centroid": list(obs.centroid),
                        "sigma_m": obs.sigma_m,
                        "zone": self.cfg.world.locate(obs.centroid),
                        "timestamp": obs.timestamp,
                    }
        self._record_zone_changes(touched)
        if self.cfg.movables is not None:
            self.events.extend(self.cfg.movables.drain_events())
        self._push_soft_references()
        from hometwin.learning import collect_ble_samples

        collect_ble_samples(self)
        return count

    def learning_status(self) -> list[dict]:
        learners = getattr(self, "_path_loss_learners", {})
        return [ln.status() for ln in learners.values()]

    def _push_soft_references(self) -> None:
        """Well-localized item tags become shared references for cameras.

        Anti-feedback gate: a camera only receives references whose tracks
        are confirmed by enough observations from sensors OTHER than itself,
        so no camera can calibrate against an estimate it produced alone.
        """
        cameras = [s for s in self.sensors if hasattr(s, "update_soft_references")]
        if not cameras:
            return
        candidates = []
        with self._lock:  # noqa: SIM117 — single guarded read of track state
            for item in self.cfg.items.all():
                track = self.engine.tracks.get(item.item_id)
                if track is None or track.sigma_m > SOFT_REF_MAX_SIGMA_M:
                    continue
                speed = math.sqrt(sum(v * v for v in track.filter.x[3:6]))
                if speed > SOFT_REF_MAX_SPEED:
                    continue
                tags = [tag for tag in item.tag_ids if tag.startswith("aruco:")]
                if tags:
                    candidates.append(
                        (tags, track.position, track.sigma_m, dict(track.contributors))
                    )
        for cam in cameras:
            refs = {}
            for tags, pos, sigma, contributors in candidates:
                others = sum(n for sid, n in contributors.items() if sid != cam.sensor_id)
                if others >= SOFT_REF_MIN_OTHER_OBS:
                    for tag in tags:
                        refs[tag] = (pos, sigma)
            cam.update_soft_references(refs)

    def _record_zone_changes(self, item_ids: set[str]) -> None:
      with self._lock:
        for item_id in item_ids:
            track = self.engine.tracks.get(item_id)
            if track is None:
                continue
            trail = self.trails.setdefault(item_id, deque(maxlen=TRAIL_LENGTH))
            pos = track.position
            if not trail or math.dist(trail[-1]["pos"], pos) >= TRAIL_MIN_STEP_M:
                trail.append({"t": track.last_update, "pos": list(pos)})
            zone = self.cfg.world.locate(track.position)
            spot = self.cfg.world.locate_spot(track.position)
            prev = self._zones.get(item_id)
            if item_id in self._zones and (zone, spot) != prev:
                self.events.append(
                    {
                        "timestamp": track.last_update,
                        "item_id": item_id,
                        "from_zone": prev[0],
                        "to_zone": zone,
                        "from_spot": prev[1],
                        "to_spot": spot,
                    }
                )
            self._zones[item_id] = (zone, spot)

    def run(self) -> None:
        period = 1.0 / self.cfg.poll_hz
        self.start_sensors()
        last_save = time.monotonic()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                self.step()
                if self.store and t0 - last_save >= self.cfg.save_interval_s:
                    self.save_state()
                    last_save = t0
                self._stop.wait(max(period - (time.monotonic() - t0), 0.0))
        finally:
            self.save_state()
            self.stop_sensors()

    def shutdown(self) -> None:
        self._stop.set()

    def snapshot(self) -> list[dict]:
        with self._lock:
            snap = self.engine.snapshot()
            self._annotate_stowed(snap)
        return snap

    def overlay_state(self) -> dict:
        """Consistent copies of everything the overlay renders. API threads
        must never iterate live tracker-thread structures (dict/deque resize
        during iteration raises) — they get this snapshot instead."""
        with self._lock:
            snap = self.engine.snapshot()
            self._annotate_stowed(snap)
            return {
                "items": snap,
                "trails": {
                    item_id: [list(p["pos"]) for p in list(trail)[-100:]]
                    for item_id, trail in self.trails.items()
                    if len(trail) >= 2
                },
                "ranges": dict(self.last_ranges),
                "bearings": dict(self.last_bearings),
                "events": list(self.events)[-20:],
                "sensor_trust": {
                    sid: round(self.engine.trust(sid), 2)
                    for sid in list(self.engine.sensor_nis)
                },
            }

    def _annotate_stowed(self, snap: list[dict]) -> None:
        """Infer "item is probably inside that drawer": last seen near the
        movable's spot while it was open, unseen since it closed."""
        if self.cfg.movables is None:
            return
        spots = {s.name: s for s in self.cfg.world.spots}
        for m in self.cfg.movables.movables:
            spot = spots.get(m.spot) if m.spot else None
            if spot is None:
                continue
            st = self.cfg.movables.states[m.name]
            if st.is_open or st.closed_ts <= st.opened_ts:
                continue  # currently open, or never cycled
            for entry in snap:
                track = self.engine.tracks.get(entry["item_id"])
                if track is None or entry.get("position") is None:
                    continue
                if (
                    track.last_update <= st.closed_ts
                    and track.last_update >= st.opened_ts - 5.0
                    and math.dist(track.position, spot.position) <= spot.radius * 1.5
                ):
                    entry["maybe_in"] = m.name

    def find(self, query: str) -> dict | None:
        """Locate one item by id or (case-insensitive) name."""
        q = query.lower()
        for entry in self.snapshot():
            if entry["item_id"].lower() == q or entry["name"].lower() == q:
                return entry
        return None

    def tag_item(self, item_id: str, tag_id: str) -> None:
        with self._lock:
            self.cfg.items.tag_item(item_id, tag_id)
