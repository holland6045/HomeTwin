"""Tags on the sensors themselves: camera-measured device positions.

A BLE scanner's `position` is hand-measured config, yet every range it
reports is anchored there — a 0.3 m survey error biases every
trilateration it touches. Stick a printed tag on the device
(`device_tag: "aruco:50"` on the sensor) and cameras measure it:

- sight rays from >= 2 camera viewpoints triangulate the device;
- the offset from the configured position is reported
  (`position_residual_m`), and with `device_tag_correct: true` folded back
  into the live sensor position, EMA-smoothed and clamped to
  `max_shift_m` from config so a misdetection can't drag a scanner across
  the room;
- a single viewpoint has no depth and therefore never corrects — rays are
  simply held until a second camera (or the same tag through the network
  bridge) provides parallax.

Device tags are consumed like movable tags: never items, never anchors.
The path-loss learner measures distances from `sensor.position`, so a
camera-corrected scanner also makes the learned RSSI model honest —
self-improvements compound.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hometwin.fusion.engine import triangulate_rays
from hometwin.observations import BearingObservation

RAY_WINDOW_S = 10.0
ALPHA = 0.25
MAX_SHIFT_M = 1.0
MIN_VIEWPOINTS = 2


@dataclass
class DeviceTagState:
    estimate: tuple[float, float, float] | None = None
    residual_m: float | None = None
    last_seen: float = 0.0
    rays: dict = field(default_factory=dict)  # camera origin key -> (obs, ts)


class DeviceTagSolver:
    def __init__(self):
        self._by_tag: dict[str, tuple[object, bool]] = {}  # tag -> (sensor, correct)
        self._configured: dict[str, tuple] = {}
        self.states: dict[str, DeviceTagState] = {}

    def register(self, tag: str, sensor, auto_correct: bool = False) -> None:
        self._by_tag[str(tag)] = (sensor, auto_correct)
        self._configured[str(tag)] = tuple(sensor.position)
        self.states[str(tag)] = DeviceTagState()

    @property
    def tags(self):
        return self._by_tag.keys()

    def observe_ray(self, tag: str, origin, direction, ts: float) -> bool:
        entry = self._by_tag.get(tag)
        if entry is None:
            return False
        sensor, auto_correct = entry
        st = self.states[tag]
        st.last_seen = ts
        key = tuple(round(o, 3) for o in origin)
        st.rays[key] = (
            BearingObservation(sensor_id="devtag", timestamp=ts,
                               origin=tuple(origin), direction=tuple(direction)),
            ts,
        )
        for k, (_, t0) in list(st.rays.items()):
            if ts - t0 > RAY_WINDOW_S:
                del st.rays[k]
        if len(st.rays) < MIN_VIEWPOINTS:
            return True
        fix = triangulate_rays([obs for obs, _ in st.rays.values()])
        if fix is None:
            return True  # parallel rays: no depth yet
        st.estimate = (
            fix if st.estimate is None
            else tuple((1 - ALPHA) * e + ALPHA * f for e, f in zip(st.estimate, fix))
        )
        configured = self._configured[tag]
        st.residual_m = math.dist(st.estimate, configured)
        if auto_correct:
            shift = math.dist(st.estimate, configured)
            target = st.estimate
            if shift > MAX_SHIFT_M:  # clamp along the correction direction
                scale = MAX_SHIFT_M / shift
                target = tuple(c + (e - c) * scale for c, e in zip(configured, st.estimate))
            sensor.position = target
        return True

    def snapshot(self) -> list[dict]:
        out = []
        for tag, (sensor, auto_correct) in self._by_tag.items():
            st = self.states[tag]
            out.append(
                {
                    "tag": tag,
                    "sensor_id": sensor.sensor_id,
                    "configured": list(self._configured[tag]),
                    "estimate": list(st.estimate) if st.estimate else None,
                    "position_residual_m": (
                        round(st.residual_m, 3) if st.residual_m is not None else None
                    ),
                    "corrected": auto_correct,
                    "viewpoints": len(st.rays),
                }
            )
        return out
