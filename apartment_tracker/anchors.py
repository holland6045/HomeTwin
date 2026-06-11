"""Fiducial calibration anchors: printed ArUco blocks at surveyed world
positions, shared as a fixed reference by every camera that can see one.

Optional for function — cameras run fine on configured poses alone. With
anchors configured, each camera that sees one compares the observed sight
ray against the known direction to the anchor:

- report mode (default): angular residual is tracked and exposed via the
  overlay/API, so a bumped camera is flagged instead of silently skewing
  fusion;
- correct mode (`anchor_correct: true`): the yaw/pitch residual is folded
  back into the camera geometry with EMA smoothing, bounded to
  `max_correction_deg` from the configured pose. Rotation drift (the
  dominant bump mode) self-heals; translation drift cannot be separated
  from rotation with sparse anchors, so a large post-correction residual
  marks the camera unhealthy and means: re-run the splat calibration.

Two cameras watching the same block share a single physical reference, so
their ray triangulations stay registered to each other and to the world
frame between scan-based recalibrations.
"""

from __future__ import annotations

import math


def _wrap(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def _az_el(d: tuple[float, float, float]) -> tuple[float, float]:
    return math.atan2(d[1], d[0]), math.atan2(d[2], math.hypot(d[0], d[1]))


class AnchorCalibrator:
    def __init__(
        self,
        geometry,
        anchors: dict[str, tuple[float, float, float]],
        auto_correct: bool = False,
        alpha: float = 0.25,
        max_correction_deg: float = 10.0,
        healthy_residual_deg: float = 1.0,
    ):
        self.geometry = geometry
        self.anchors = {tag: tuple(pos) for tag, pos in anchors.items()}
        self.auto_correct = auto_correct
        self.alpha = alpha
        self.max_correction = math.radians(max_correction_deg)
        self.healthy_residual_deg = healthy_residual_deg
        self._yaw0 = geometry.yaw
        self._pitch0 = geometry.pitch
        self.residual_deg: float | None = None  # EMA of angular residual magnitude
        self.last_seen: dict[str, float] = {}

    def observe(self, tag_id: str, u: float, v: float, ts: float) -> bool:
        """Process one detection. Returns True when the tag is an anchor
        (caller must then NOT emit it as an item observation)."""
        pos = self.anchors.get(tag_id)
        if pos is None:
            return False
        geo = self.geometry
        dx, dy, dz = (pos[i] - geo.position[i] for i in range(3))
        n = math.sqrt(dx * dx + dy * dy + dz * dz)
        if n < 1e-6:
            return True
        az_exp, el_exp = _az_el((dx / n, dy / n, dz / n))
        az_obs, el_obs = _az_el(geo.ray(u, v))
        raz = _wrap(az_obs - az_exp)
        rel = el_obs - el_exp

        self.last_seen[tag_id] = ts
        magnitude = math.degrees(math.hypot(raz, rel))
        self.residual_deg = (
            magnitude
            if self.residual_deg is None
            else (1 - self.alpha) * self.residual_deg + self.alpha * magnitude
        )

        if self.auto_correct:
            # config yaw off by D shows up as raz = -D; pitch by D as rel = +D
            geo.yaw = self._clamp(geo.yaw - self.alpha * raz, self._yaw0)
            geo.pitch = self._clamp(geo.pitch + self.alpha * rel, self._pitch0)
        return True

    def _clamp(self, value: float, reference: float) -> float:
        return max(reference - self.max_correction, min(reference + self.max_correction, value))

    def status(self) -> dict:
        return {
            "anchors": sorted(self.last_seen),
            "configured": sorted(self.anchors),
            "residual_deg": round(self.residual_deg, 3) if self.residual_deg is not None else None,
            "correction_yaw_deg": round(math.degrees(self.geometry.yaw - self._yaw0), 3),
            "correction_pitch_deg": round(math.degrees(self.geometry.pitch - self._pitch0), 3),
            "auto_correct": self.auto_correct,
            "healthy": (
                self.residual_deg is None or self.residual_deg < self.healthy_residual_deg
            ),
        }
