"""Tracker orchestrator: polls every sensor, feeds the fusion engine, and
keeps the queryable state the API/CLI expose.

One sensor failing (raising in poll) is logged and skipped — never fatal.
Presence-only observations (no item resolution) are kept separately so
occupancy is queryable even though it fuses into no item track.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

from apartment_tracker.config import AppConfig
from apartment_tracker.fusion import FusionEngine
from apartment_tracker.observations import (
    AreaObservation,
    BearingObservation,
    RangeObservation,
)
from apartment_tracker.store import StateStore

log = logging.getLogger("apartment_tracker")

EVENT_HISTORY = 500


class Tracker:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.engine = FusionEngine(cfg.items, cfg.world, stale_after_s=cfg.stale_after_s)
        self.sensors = cfg.sensors
        self.presence: dict | None = None
        self.events: deque[dict] = deque(maxlen=EVENT_HISTORY)
        self.last_ranges: dict[tuple[str, str], dict] = {}  # (sensor, item) -> last range
        self.last_bearings: dict[tuple[str, str], dict] = {}  # (sensor, item) -> last ray
        self._zones: dict[str, str | None] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.store = StateStore(cfg.state_path) if cfg.state_path else None
        if self.store:
            restored = self.engine.restore_state(self.store.load())
            if restored:
                log.info("restored %d track(s) from %s", restored, cfg.state_path)
            # restored locations are the zone baseline, not "arrival" events
            for t in self.engine.tracks.values():
                self._zones[t.item_id] = cfg.world.locate(t.position)

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
        return count

    def _record_zone_changes(self, item_ids: set[str]) -> None:
        for item_id in item_ids:
            track = self.engine.tracks.get(item_id)
            if track is None:
                continue
            zone = self.cfg.world.locate(track.position)
            prev = self._zones.get(item_id)
            if item_id in self._zones and zone != prev:
                self.events.append(
                    {
                        "timestamp": track.last_update,
                        "item_id": item_id,
                        "from_zone": prev,
                        "to_zone": zone,
                    }
                )
            self._zones[item_id] = zone

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
            return self.engine.snapshot()

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
