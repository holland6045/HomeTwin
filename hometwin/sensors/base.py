"""Sensor adapter contract.

An adapter wraps one physical source (a camera, a BLE scanner, a tomography
node mesh, an MCU bridge socket) and yields Observations on poll(). Adapters
must be non-blocking: poll() returns whatever is ready and comes back later
for more. All hardware I/O, threading, and protocol parsing stay inside the
adapter — the tracker loop only ever sees Observation objects.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod

from hometwin.observations import Observation


class SensorAdapter(ABC):
    def __init__(self, sensor_id: str):
        self.sensor_id = sensor_id
        # observation timestamp source; replace for simulation/replay
        self.clock = time.time

    def start(self) -> None:
        """Open hardware / spawn worker threads. Default: nothing."""

    def stop(self) -> None:
        """Release hardware. Default: nothing."""

    @abstractmethod
    def poll(self) -> list[Observation]:
        """Return observations ready since the last call. Must not block."""

    def overlay(self) -> dict | None:
        """Optional renderable layer for the visualization UI.

        Sensors that can be drawn (tomography heat maps, camera poses)
        return a dict with a "kind" key; everything else returns None.
        """
        return None
