"""Apartment world model: named zones over a shared world frame.

World frame: meters, right-handed, z up, origin at a corner of the apartment
chosen during setup. Every sensor pose and every fused estimate lives in this
frame; zones turn coordinates into human answers ("kitchen counter").
"""

from __future__ import annotations

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


class World:
    def __init__(self, zones: list[Zone] | None = None):
        self.zones = zones or []

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

    @classmethod
    def from_config(cls, zone_cfgs: list[dict]) -> "World":
        return cls(
            [
                Zone(c["name"], tuple(c["min"]), tuple(c["max"]))
                for c in zone_cfgs
            ]
        )
