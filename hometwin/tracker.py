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
from concurrent.futures import ThreadPoolExecutor

from hometwin.config import AppConfig
from hometwin.fusion import FusionEngine
from hometwin.observations import (
    AreaObservation,
    BearingObservation,
    PositionObservation,
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
        self._wm_movable_seen: dict[str, tuple] = {}
        self._stop = threading.Event()
        # RLock: overlay/snapshot helpers nest under step()'s critical section
        self._lock = threading.RLock()
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: dict = {}
        if getattr(cfg, "parallel_polling", True) and len(cfg.sensors) > 1:
            from hometwin.accel import poll_workers

            self._pool = ThreadPoolExecutor(
                max_workers=poll_workers(len(cfg.sensors)),
                thread_name_prefix="hometwin-poll",
            )
        from hometwin.worldmodel import WorldModel

        self._wm_path = f"{cfg.state_path}.worldmodel.gz" if cfg.state_path else None
        self.worldmodel = (
            WorldModel.load(self._wm_path) if self._wm_path else WorldModel()
        )
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
        if self._wm_path:
            self.worldmodel.decay_and_compact(
                self.worldmodel.maintenance_now(time.time())
            )
            self.worldmodel.save(self._wm_path)

    def start_sensors(self) -> None:
        for s in self.sensors:
            s.start()

    def stop_sensors(self) -> None:
        for s in self.sensors:
            try:
                s.stop()
            except Exception:
                log.exception("sensor %s failed to stop", s.sensor_id)

    def _poll_all(self) -> list[tuple]:
        """Poll every sensor, in parallel when a pool exists. Polling is
        I/O + C-extension bound (sockets, cv2, onnxruntime — all release
        the GIL), so cameras stop serializing behind each other. Fusion
        stays single-threaded: batches come back in sensor order."""
        if self._pool is None:
            results = []
            for sensor in self.sensors:
                try:
                    results.append((sensor, sensor.poll()))
                except Exception:
                    log.exception("sensor %s poll failed", sensor.sensor_id)
            return results
        # a sensor whose previous poll is still running (hung MJPEG read)
        # is skipped this round rather than stacking tasks until the pool
        # starves every healthy sensor
        futures = []
        for s in self.sensors:
            prev = self._inflight.get(s.sensor_id)
            if prev is not None and not prev.done():
                continue
            fut = self._pool.submit(s.poll)
            self._inflight[s.sensor_id] = fut
            futures.append((s, fut))
        results = []
        for sensor, fut in futures:
            try:
                results.append((sensor, fut.result(timeout=10.0)))
            except Exception:
                log.exception("sensor %s poll failed", sensor.sensor_id)
        return results

    def step(self) -> int:
        """Poll all sensors once and fuse. Returns observations processed."""
        count = 0
        touched: set[str] = set()
        for _sensor, batch in self._poll_all():
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
                    self.worldmodel.add_point(obs.centroid, obs.timestamp, weight=0.3)
                    self._motion_evidence(obs.centroid, obs.timestamp, obs.sigma_m)
                    self.presence = {
                        "sensor_id": obs.sensor_id,
                        "centroid": list(obs.centroid),
                        "sigma_m": obs.sigma_m,
                        "zone": self.cfg.world.locate(obs.centroid),
                        "timestamp": obs.timestamp,
                    }
                elif (
                    isinstance(obs, PositionObservation)
                    and obs.label in self.cfg.presence_labels
                ):
                    # camera person-detections that map to no item are
                    # occupancy: they drive motion zones, the presence
                    # display, and the world model — synthetic PIRs work
                    # from a webcam alone
                    self.worldmodel.add_point(obs.position, obs.timestamp, weight=0.3)
                    self._motion_evidence(obs.position, obs.timestamp, obs.sigma_m)
                    self.presence = {
                        "sensor_id": obs.sensor_id,
                        "centroid": list(obs.position),
                        "sigma_m": obs.sigma_m,
                        "zone": self.cfg.world.locate(obs.position),
                        "timestamp": obs.timestamp,
                    }
        self._record_zone_changes(touched)
        if self.cfg.movables is not None:
            for event in self.cfg.movables.events:
                # a drawer/door physically moving IS motion at its location
                movable = next(
                    (m for m in self.cfg.movables.movables
                     if m.name == event.get("movable")), None)
                if movable is not None:
                    st = self.cfg.movables.states[movable.name]
                    self._motion_evidence(
                        movable.position_at(st.openness), event["timestamp"], 0.3)
            for m in self.cfg.movables.movables:
                st = self.cfg.movables.states[m.name]
                if st.last_seen and st.last_seen not in self._wm_movable_seen.get(m.name, ()):
                    self.worldmodel.add_point(
                        m.position_at(st.openness), st.last_seen, weight=0.5
                    )
                    self._wm_movable_seen[m.name] = (st.last_seen,)
            self.events.extend(self.cfg.movables.drain_events())
        self._push_soft_references()
        from hometwin.learning import collect_ble_samples

        collect_ble_samples(self)
        self._expire_motion_zones()
        return count

    def _motion_evidence(self, p, ts: float, sigma_m: float = 0.0) -> None:
        mz = self.cfg.motion_zones
        if mz is None:
            return
        self._motion_clock = max(getattr(self, "_motion_clock", 0.0), ts)
        for zone in mz.evidence(p, ts, sigma_m):
            self.events.append({"timestamp": ts, "motion_zone": zone.name, "state": "ON"})
            if self.cfg.hass is not None:
                self.cfg.hass.publish_state(zone)

    def _expire_motion_zones(self) -> None:
        mz = self.cfg.motion_zones
        if mz is None:
            return
        # live: expire on wall time so zones clear even when evidence stops
        # entirely; sim/replay (evidence clock far from wall) expires on the
        # evidence clock so synthetic runs behave deterministically
        wall = time.time()
        clock = getattr(self, "_motion_clock", 0.0)
        now = wall if wall - clock < 3600.0 else clock
        for zone in mz.expire(now):
            self.events.append(
                {"timestamp": now, "motion_zone": zone.name, "state": "OFF"})
            if self.cfg.hass is not None:
                self.cfg.hass.publish_state(zone)

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
            # the passive world model sketches space from positions the
            # system already trusts; weight by track confidence
            self.worldmodel.add_point(
                pos, track.last_update, weight=1.0 / (1.0 + track.sigma_m)
            )
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
            if self._pool is not None:
                self._pool.shutdown(wait=False)

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

    def add_anchor(self, tag: str, position: tuple) -> None:
        """Drop a calibration anchor at runtime (dashboard/board-check)."""
        with self._lock:
            self.cfg.anchors.setdefault(tag, []).append(tuple(position))
            for sensor in self.sensors:
                if not hasattr(sensor, "attach_anchors"):
                    continue
                if getattr(sensor, "calibrator", None) is None:
                    sensor.attach_anchors({tag: [tuple(position)]})
                else:
                    sensor.calibrator.anchors.setdefault(tag, []).append(
                        tuple(position)
                    )

    def tag_item(self, item_id: str, tag_id: str) -> None:
        with self._lock:
            self.cfg.items.tag_item(item_id, tag_id)

    def reconfigure_camera(self, sensor_id: str, source_cfg: dict) -> dict:
        """Hot-swap a camera's capture settings (device/resolution/fps) and
        persist them to the config sidecar so restarts keep them. Returns
        the newly negotiated mode. Raises KeyError for unknown cameras."""
        from hometwin import registry

        cam = next((s for s in self.sensors
                    if getattr(s, "sensor_id", None) == sensor_id
                    and hasattr(s, "frame_source")), None)
        if cam is None:
            raise KeyError(f"unknown camera {sensor_id!r}")
        old = cam.frame_source
        merged = {k: getattr(old, k)
                  for k in ("device", "width", "height", "fps", "fourcc")
                  if hasattr(old, k)}
        merged.update(source_cfg)
        new = registry.create("frame_source", "opencv", **merged)
        # release the device before the replacement opens it (Windows
        # capture is exclusive); roll back if the new mode won't open
        if hasattr(old, "stop"):
            old.stop()
        try:
            new.start()
        except Exception:
            if hasattr(old, "start"):
                old.start()
            raise
        cam.frame_source = new
        self._persist_override(sensor_id, "source", merged)
        return new.describe()

    def _persist_override(self, sensor_id: str, key: str, value) -> None:
        path = getattr(self.cfg, "overrides_path", None)
        if not path:
            return
        import json
        import os
        from pathlib import Path

        p = Path(path)
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8")) or {}
            except ValueError:
                data = {}
        data.setdefault("sensors", {}).setdefault(sensor_id, {})[key] = value
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
