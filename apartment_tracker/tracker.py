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

from apartment_tracker.config import AppConfig
from apartment_tracker.fusion import FusionEngine
from apartment_tracker.observations import AreaObservation

log = logging.getLogger("apartment_tracker")


class Tracker:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.engine = FusionEngine(cfg.items, cfg.world, stale_after_s=cfg.stale_after_s)
        self.sensors = cfg.sensors
        self.presence: dict | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

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
                if applied is None and isinstance(obs, AreaObservation):
                    self.presence = {
                        "sensor_id": obs.sensor_id,
                        "centroid": list(obs.centroid),
                        "sigma_m": obs.sigma_m,
                        "zone": self.cfg.world.locate(obs.centroid),
                        "timestamp": obs.timestamp,
                    }
        return count

    def run(self) -> None:
        period = 1.0 / self.cfg.poll_hz
        self.start_sensors()
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                self.step()
                self._stop.wait(max(period - (time.monotonic() - t0), 0.0))
        finally:
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
