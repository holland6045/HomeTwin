"""RF tomography (radio tomographic imaging).

A mesh of cheap radio nodes (ESP32/nRF/Zigbee — anything that reports
per-link RSS) rings a space. A body or large object on a link's path
attenuates it; back-projecting per-link attenuation over an ellipse weight
model reconstructs a coarse occupancy image, no wearables required.

Output is an AreaObservation (centroid + spread). By default it carries the
label "presence" — useful on its own for occupancy and, when an item is
known to be carried, as a coarse fusion input. Identity never comes from
tomography; it comes from tags.

LinkSource contract: readings() -> [(node_a, node_b, rss_dbm), ...]. Any
transport works: serial, ESP-NOW gateway, the network bridge, MQTT.
"""

from __future__ import annotations

import math
import time

from apartment_tracker.observations import AreaObservation
from apartment_tracker.registry import register
from apartment_tracker.sensors.base import SensorAdapter


class LinkSource:
    def readings(self) -> list[tuple[str, str, float]]:
        raise NotImplementedError


class RTIGrid:
    """Ellipse-weighted back-projection onto a 2D grid at a fixed height."""

    def __init__(
        self,
        nodes: dict[str, tuple[float, float]],
        bounds: tuple[float, float, float, float],  # xmin, ymin, xmax, ymax
        cell_m: float = 0.25,
        lambda_m: float = 0.3,  # ellipse excess-path width
    ):
        self.nodes = {k: tuple(v) for k, v in nodes.items()}
        self.xmin, self.ymin, self.xmax, self.ymax = bounds
        self.cell = cell_m
        self.lam = lambda_m
        self.nx = max(int(round((self.xmax - self.xmin) / cell_m)), 1)
        self.ny = max(int(round((self.ymax - self.ymin) / cell_m)), 1)

    def cell_center(self, ix: int, iy: int) -> tuple[float, float]:
        return (self.xmin + (ix + 0.5) * self.cell, self.ymin + (iy + 0.5) * self.cell)

    def reconstruct(self, attenuations: dict[tuple[str, str], float]) -> list[list[float]]:
        img = [[0.0] * self.nx for _ in range(self.ny)]
        for (a, b), atten in attenuations.items():
            if atten <= 0 or a not in self.nodes or b not in self.nodes:
                continue
            ax, ay = self.nodes[a]
            bx, by = self.nodes[b]
            link_len = math.hypot(bx - ax, by - ay)
            if link_len < 1e-6:
                continue
            w = atten / math.sqrt(link_len)
            for iy in range(self.ny):
                for ix in range(self.nx):
                    px, py = self.cell_center(ix, iy)
                    excess = (
                        math.hypot(px - ax, py - ay)
                        + math.hypot(px - bx, py - by)
                        - link_len
                    )
                    if excess < self.lam:
                        img[iy][ix] += w
        return img

    def blob(
        self, img: list[list[float]], threshold_frac: float = 0.6
    ) -> tuple[tuple[float, float], float, float] | None:
        """Weighted centroid of cells above threshold: ((x, y), spread_m, peak)."""
        peak = max(max(row) for row in img)
        if peak <= 0:
            return None
        thr = peak * threshold_frac
        wsum = sx = sy = 0.0
        cells = []
        for iy in range(self.ny):
            for ix in range(self.nx):
                v = img[iy][ix]
                if v >= thr:
                    px, py = self.cell_center(ix, iy)
                    wsum += v
                    sx += v * px
                    sy += v * py
                    cells.append((px, py, v))
        cx, cy = sx / wsum, sy / wsum
        var = sum(v * ((px - cx) ** 2 + (py - cy) ** 2) for px, py, v in cells) / wsum
        return ((cx, cy), max(math.sqrt(var), self.cell), peak)


@register("sensor", "rf_tomography")
class TomographySensor(SensorAdapter):
    def __init__(
        self,
        sensor_id: str,
        nodes: dict[str, tuple[float, float]],
        bounds: tuple[float, float, float, float],
        source: LinkSource | None = None,
        cell_m: float = 0.25,
        lambda_m: float = 0.3,
        height_m: float = 1.0,
        label: str = "presence",
        min_attenuation_db: float = 2.0,
    ):
        super().__init__(sensor_id)
        self.grid = RTIGrid(nodes, tuple(bounds), cell_m, lambda_m)
        self.source = source
        self.height_m = height_m
        self.label = label
        self.min_atten = min_attenuation_db
        self._baseline: dict[tuple[str, str], float] = {}
        self._last_image: list[list[float]] | None = None
        self._last_image_ts: float = 0.0

    @staticmethod
    def _key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def overlay(self) -> dict:
        g = self.grid
        peak = (
            max(max(row) for row in self._last_image) if self._last_image else 0.0
        )
        return {
            "kind": "heatmap",
            "sensor_id": self.sensor_id,
            "bounds": [g.xmin, g.ymin, g.xmax, g.ymax],
            "cell_m": g.cell,
            "height_m": self.height_m,
            "timestamp": self._last_image_ts,
            "values": (
                [[round(v / peak, 3) for v in row] for row in self._last_image]
                if self._last_image and peak > 0
                else None
            ),
            "nodes": {k: list(v) for k, v in self.grid.nodes.items()},
        }

    def calibrate(self, readings: list[tuple[str, str, float]]) -> None:
        """Record empty-room RSS baselines. Re-run whenever furniture moves."""
        for a, b, rss in readings:
            self._baseline[self._key(a, b)] = rss

    def poll(self) -> list[AreaObservation]:
        if self.source is None:
            return []
        atten: dict[tuple[str, str], float] = {}
        for a, b, rss in self.source.readings():
            key = self._key(a, b)
            base = self._baseline.get(key)
            if base is None:
                # first sighting of a link doubles as its baseline
                self._baseline[key] = rss
                continue
            loss = base - rss
            if loss >= self.min_atten:
                atten[key] = loss
        if not atten:
            self._last_image = None
            return []
        img = self.grid.reconstruct(atten)
        self._last_image = img
        self._last_image_ts = time.time()
        result = self.grid.blob(img)
        if result is None:
            return []
        (cx, cy), spread, peak = result
        return [
            AreaObservation(
                sensor_id=self.sensor_id,
                timestamp=time.time(),
                label=self.label,
                confidence=min(peak / 10.0, 1.0),
                centroid=(cx, cy, self.height_m),
                sigma_m=max(spread, 0.5),
            )
        ]
