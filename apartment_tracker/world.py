"""Apartment world model: named zones and spots over a shared world frame.

World frame: meters, right-handed, z up, origin at a corner of the apartment
chosen during setup. Every sensor pose and every fused estimate lives in this
frame. Zones turn coordinates into room-level answers ("kitchen"); spots are
precise micro-locations — an individual drawer, shelf, bin, or hook — that
resolve a position to "desk-drawer-2" when the estimate lands within the
spot's radius. A spot may carry a printed tag (see `apartment_tracker.tags`),
in which case its surveyed position also serves as a camera calibration
anchor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Zone:
    name: str
    min_corner: tuple[float, float, float]
    max_corner: tuple[float, float, float]

    def contains(self, p: tuple[float, float, float]) -> bool:
        return all(lo <= v <= hi for v, lo, hi in zip(p, self.min_corner, self.max_corner))

    @property
    def center(self) -> tuple[float, float, float]:
        return tuple((lo + hi) / 2.0 for lo, hi in zip(self.min_corner, self.max_corner))


@dataclass
class Spot:
    name: str
    position: tuple[float, float, float]
    radius: float = 0.25
    tag: str | None = None  # e.g. "aruco:13" — doubles as a calibration anchor

    def contains(self, p: tuple[float, float, float]) -> bool:
        return math.dist(p, self.position) <= self.radius


class World:
    def __init__(self, zones: list[Zone] | None = None, spots: list[Spot] | None = None):
        self.zones = zones or []
        self.spots = spots or []

    def add_zone(self, zone: Zone) -> None:
        self.zones.append(zone)

    def locate(self, p: tuple[float, float, float]) -> str | None:
        """Most specific (smallest-volume) zone containing p, or None."""
        hits = [z for z in self.zones if z.contains(p)]
        if not hits:
            return None

        def volume(z: Zone) -> float:
            return (
                (z.max_corner[0] - z.min_corner[0])
                * (z.max_corner[1] - z.min_corner[1])
                * max(z.max_corner[2] - z.min_corner[2], 1e-6)
            )

        return min(hits, key=volume).name

    def locate_spot(self, p: tuple[float, float, float]) -> str | None:
        """Tightest (smallest-radius) spot containing p, or None."""
        hits = [s for s in self.spots if s.contains(p)]
        return min(hits, key=lambda s: s.radius).name if hits else None

    @classmethod
    def from_config(cls, zone_cfgs: list[dict], spot_cfgs: list[dict] | None = None) -> "World":
        return cls(
            [Zone(c["name"], tuple(c["min"]), tuple(c["max"])) for c in zone_cfgs],
            [
                Spot(
                    c["name"],
                    tuple(c["position"]),
                    float(c.get("radius", 0.25)),
                    c.get("tag"),
                )
                for c in (spot_cfgs or [])
            ],
        )
