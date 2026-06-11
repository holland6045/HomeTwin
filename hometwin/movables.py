"""Tags on moving surfaces: doors, drawers, cabinet fronts.

A tag on a door is neither a fixed anchor (it would poison camera
calibration) nor an item (it never leaves its track). It is a one-DOF
mechanism with a known motion model:

- slide: the tag moves along `home + t*axis`, t in [0, travel]  (drawers)
- hinge: the tag swings on a horizontal arc of radius |home-hinge| about a
  vertical hinge line, angle in [0, max_angle_deg]              (doors)

Each camera sighting is a ray; projecting it onto the motion path yields
the openness fraction. That makes every tagged door/drawer an open/closed
sensor with events — and lets the tracker infer "item X was last seen
inside drawer Y while it was open, and Y closed: X is probably in Y".

Movable tags are excluded from the anchor map by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

OPEN_ABOVE = 0.2  # hysteresis: open when fraction rises above,
CLOSE_BELOW = 0.1  # closed when it falls below
HINGE_SAMPLES = 64


@dataclass
class Movable:
    name: str
    tag: str
    motion: str  # "slide" | "hinge"
    home: tuple[float, float, float]  # tag position when fully closed
    axis: tuple[float, float, float] | None = None  # slide direction (unit not required)
    travel: float = 0.0  # slide extension, m
    hinge: tuple[float, float] | None = None  # vertical hinge line (x, y)
    max_angle_deg: float = 0.0  # signed swing; sign sets direction
    spot: str | None = None  # spot whose contents this movable encloses

    def __post_init__(self) -> None:
        if self.motion not in ("slide", "hinge"):
            raise ValueError(f"movable {self.name!r}: motion must be slide or hinge")
        if self.motion == "slide":
            if not self.axis or self.travel <= 0:
                raise ValueError(f"movable {self.name!r}: slide needs axis and travel > 0")
            n = math.sqrt(sum(a * a for a in self.axis))
            self.axis = tuple(a / n for a in self.axis)
        else:
            if not self.hinge or self.max_angle_deg == 0:
                raise ValueError(f"movable {self.name!r}: hinge needs hinge point and max_angle_deg")

    def position_at(self, fraction: float) -> tuple[float, float, float]:
        f = max(0.0, min(1.0, fraction))
        if self.motion == "slide":
            return tuple(h + f * self.travel * a for h, a in zip(self.home, self.axis))
        ang = math.radians(f * self.max_angle_deg)
        hx, hy = self.hinge
        rx, ry = self.home[0] - hx, self.home[1] - hy
        c, s = math.cos(ang), math.sin(ang)
        return (hx + rx * c - ry * s, hy + rx * s + ry * c, self.home[2])

    def path(self, samples: int = 9) -> list[tuple[float, float, float]]:
        return [self.position_at(i / (samples - 1)) for i in range(samples)]


@dataclass
class MovableState:
    openness: float = 0.0
    is_open: bool = False
    last_seen: float = 0.0
    opened_ts: float = 0.0
    closed_ts: float = 0.0


def _ray_point_dist(origin, direction, p) -> float | None:
    w = tuple(p[i] - origin[i] for i in range(3))
    along = sum(w[i] * direction[i] for i in range(3))
    if along <= 0:
        return None  # behind the camera
    perp2 = sum(w[i] * w[i] for i in range(3)) - along * along
    return math.sqrt(max(perp2, 0.0))


class MovableRegistry:
    def __init__(self, movables: list[Movable], alpha: float = 0.4):
        self.movables = list(movables)
        self.by_tag = {m.tag: m for m in movables}
        self.states: dict[str, MovableState] = {m.name: MovableState() for m in movables}
        self.alpha = alpha
        self.events: list[dict] = []  # drained by the tracker

    def observe_ray(self, tag: str, origin, direction, ts: float) -> bool:
        m = self.by_tag.get(tag)
        if m is None:
            return False
        fraction = (
            self._slide_fraction(m, origin, direction)
            if m.motion == "slide"
            else self._hinge_fraction(m, origin, direction)
        )
        if fraction is None:
            return True  # consumed, but unusable geometry this frame
        st = self.states[m.name]
        st.openness = (1 - self.alpha) * st.openness + self.alpha * fraction
        st.last_seen = ts
        if not st.is_open and st.openness > OPEN_ABOVE:
            st.is_open = True
            st.opened_ts = ts
            self.events.append(
                {"timestamp": ts, "movable": m.name, "event": "opened",
                 "openness": round(st.openness, 2)}
            )
        elif st.is_open and st.openness < CLOSE_BELOW:
            st.is_open = False
            st.closed_ts = ts
            self.events.append(
                {"timestamp": ts, "movable": m.name, "event": "closed",
                 "openness": round(st.openness, 2)}
            )
        return True

    @staticmethod
    def _slide_fraction(m: Movable, origin, direction) -> float | None:
        """Closest point between the sight ray and the slide segment."""
        w = tuple(origin[i] - m.home[i] for i in range(3))
        b = sum(direction[i] * m.axis[i] for i in range(3))
        dw = sum(direction[i] * w[i] for i in range(3))
        uw = sum(m.axis[i] * w[i] for i in range(3))
        denom = 1.0 - b * b
        if denom < 1e-9:
            return None  # ray parallel to the slide: no information
        t = (uw - b * dw) / denom
        s = t * b - dw
        if s <= 0:
            return None
        return max(0.0, min(1.0, t / m.travel))

    @staticmethod
    def _hinge_fraction(m: Movable, origin, direction) -> float | None:
        """Arc angle minimizing ray-to-tag distance (coarse sample, cheap)."""
        best_f, best_d = None, float("inf")
        for i in range(HINGE_SAMPLES + 1):
            f = i / HINGE_SAMPLES
            d = _ray_point_dist(origin, direction, m.position_at(f))
            if d is not None and d < best_d:
                best_f, best_d = f, d
        return best_f

    def drain_events(self) -> list[dict]:
        out, self.events = self.events, []
        return out

    def snapshot(self) -> list[dict]:
        out = []
        for m in self.movables:
            st = self.states[m.name]
            out.append(
                {
                    "name": m.name,
                    "tag": m.tag,
                    "motion": m.motion,
                    "openness": round(st.openness, 3),
                    "is_open": st.is_open,
                    "last_seen": st.last_seen,
                    "spot": m.spot,
                    "path": [list(p) for p in m.path()],
                    "tag_pos": list(m.position_at(st.openness)),
                }
            )
        return out

    @classmethod
    def from_config(cls, cfgs: list[dict]) -> "MovableRegistry":
        return cls(
            [
                Movable(
                    name=c["name"],
                    tag=str(c["tag"]),
                    motion=c["motion"],
                    home=tuple(c["home"]),
                    axis=tuple(c["axis"]) if c.get("axis") else None,
                    travel=float(c.get("travel", 0.0)),
                    hinge=tuple(c["hinge"]) if c.get("hinge") else None,
                    max_angle_deg=float(c.get("max_angle_deg", 0.0)),
                    spot=c.get("spot"),
                )
                for c in cfgs
            ]
        )
