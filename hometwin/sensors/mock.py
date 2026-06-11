"""Scripted sensors for tests and the simulation harness."""

from __future__ import annotations

from collections import deque

from hometwin.observations import Observation
from hometwin.registry import register
from hometwin.sensors.base import SensorAdapter


@register("sensor", "scripted")
class ScriptedSensor(SensorAdapter):
    """Replays a pre-built observation sequence, a batch per poll."""

    def __init__(self, sensor_id: str, observations: list[list[Observation]] | None = None):
        super().__init__(sensor_id)
        self._batches: deque[list[Observation]] = deque(observations or [])

    def push(self, batch: list[Observation]) -> None:
        self._batches.append(batch)

    def poll(self) -> list[Observation]:
        return self._batches.popleft() if self._batches else []
