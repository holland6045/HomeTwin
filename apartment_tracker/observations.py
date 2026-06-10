"""Sensor-agnostic observation types.

Every sensing modality reduces to one of these before fusion. New hardware
never touches the fusion engine — it only has to emit one of these shapes:

- PositionObservation: full 3D fix (camera + known geometry, UWB, ...)
- RangeObservation:    distance from a known anchor (BLE RSSI, UWB, acoustic)
- BearingObservation:  a sight ray from a known origin (camera without a
                       surface assumption, directional antenna) — one ray
                       constrains direction, two viewpoints fix 3D
- AreaObservation:     diffuse blob with a centroid + spread (RF tomography,
                       PIR zones, pressure mats)

`item_id` is set when the sensor identifies the tag directly (ArUco ID,
BLE MAC, RFID EPC). Anonymous detections carry `label` (e.g. "keys") and are
associated to tracks by the fusion engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Observation:
    sensor_id: str
    timestamp: float
    item_id: str | None = None
    label: str | None = None
    confidence: float = 1.0


@dataclass
class PositionObservation(Observation):
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    sigma_m: float = 0.3  # 1-sigma position error, meters


@dataclass
class RangeObservation(Observation):
    anchor: tuple[float, float, float] = (0.0, 0.0, 0.0)
    range_m: float = 0.0
    sigma_m: float = 1.0


@dataclass
class BearingObservation(Observation):
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    direction: tuple[float, float, float] = (1.0, 0.0, 0.0)  # unit, world frame
    sigma_rad: float = 0.02


@dataclass
class AreaObservation(Observation):
    centroid: tuple[float, float, float] = (0.0, 0.0, 0.0)
    sigma_m: float = 2.0


@dataclass
class Detection:
    """Raw 2D detector output, prior to projection into world space."""

    label: str
    confidence: float
    bbox: tuple[float, float, float, float]  # x, y, w, h normalized 0-1
    tag_id: str | None = None
    extras: dict = field(default_factory=dict)

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.bbox
        return (x + w / 2.0, y + h / 2.0)
