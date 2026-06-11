"""Auto-benchmark for newly onboarded MCU nodes.

Every node that talks to the bridge gets profiled, two ways:

- **Passive** (no firmware support needed): message rate, inter-arrival
  jitter, message-type mix, and per-MAC RSSI distributions — the latter is
  the radio/antenna character of the node (a board with a cracked antenna
  shows as a low, noisy RSSI envelope immediately).
- **Active** (node sends `{"type": "hello", ...caps}` on connect): the
  bridge answers with a burst of `{"type": "ping", "seq": n}` lines on the
  same socket; the node echoes pongs. RTT min/mean/max and loss establish
  the node's real round-trip envelope — what its watchdog deadlines and
  poll budgets should be sized against.

Profiles live on the bridge, surface at /overlay/map -> devices, and
persist for the life of the process. Hello caps (chip, firmware, rssi
chipset, ...) are stored verbatim — the schema belongs to the node.
"""

from __future__ import annotations

import math
import time
from collections import Counter, deque

ARRIVAL_WINDOW = 100
PING_COUNT = 20
PING_TIMEOUT_S = 10.0


class RSSIStats:
    def __init__(self):
        self.count = 0
        self.mean = 0.0
        self._m2 = 0.0
        self.min = float("inf")
        self.max = float("-inf")

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self._m2 += delta * (value - self.mean)
        self.min = min(self.min, value)
        self.max = max(self.max, value)

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "mean": round(self.mean, 1),
            "std": round(math.sqrt(self._m2 / self.count), 2) if self.count > 1 else 0.0,
            "min": self.min,
            "max": self.max,
        }


class DeviceProfile:
    def __init__(self, sensor_id: str, clock=time.time):
        self.sensor_id = sensor_id
        self.clock = clock
        self.first_seen = clock()
        self.messages = 0
        self.types: Counter = Counter()
        self.arrivals: deque[float] = deque(maxlen=ARRIVAL_WINDOW)
        self.rssi: dict[str, RSSIStats] = {}
        self.caps: dict = {}
        # echo benchmark state
        self.ping_sent: dict[int, float] = {}
        self.rtts_ms: list[float] = []
        self.bench_started: float | None = None

    def note_message(self, msg: dict) -> None:
        self.messages += 1
        self.types[msg.get("type", "?")] += 1
        self.arrivals.append(self.clock())
        if msg.get("type") == "rssi" and "mac" in msg and "rssi" in msg:
            mac = str(msg["mac"]).upper()
            self.rssi.setdefault(mac, RSSIStats()).add(float(msg["rssi"]))

    def start_benchmark(self, caps: dict) -> list[str]:
        """Hello received: store caps, emit the ping burst to write back."""
        self.caps = {k: v for k, v in caps.items() if k not in ("type", "auth")}
        self.bench_started = self.clock()
        self.ping_sent.clear()
        self.rtts_ms.clear()
        lines = []
        for seq in range(PING_COUNT):
            self.ping_sent[seq] = self.clock()
            lines.append('{"type": "ping", "seq": %d}' % seq)
        return lines

    def note_pong(self, seq: int) -> None:
        sent = self.ping_sent.pop(int(seq), None)
        if sent is not None:
            self.rtts_ms.append((self.clock() - sent) * 1000.0)

    def _rate_stats(self) -> dict:
        if len(self.arrivals) < 2:
            return {"rate_hz": 0.0, "jitter_ms": None}
        gaps = [b - a for a, b in zip(self.arrivals, list(self.arrivals)[1:])]
        mean = sum(gaps) / len(gaps)
        if mean <= 0:
            return {"rate_hz": 0.0, "jitter_ms": None}
        var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        return {"rate_hz": round(1.0 / mean, 2), "jitter_ms": round(math.sqrt(var) * 1000, 1)}

    def echo_stats(self) -> dict | None:
        if self.bench_started is None:
            return None
        pending = len(self.ping_sent)
        timed_out = (
            pending > 0 and self.clock() - self.bench_started > PING_TIMEOUT_S
        )
        out = {
            "sent": PING_COUNT,
            "received": len(self.rtts_ms),
            "loss": round(1.0 - len(self.rtts_ms) / PING_COUNT, 2),
            "complete": pending == 0 or timed_out,
        }
        if self.rtts_ms:
            rtts = sorted(self.rtts_ms)
            out.update(
                rtt_ms_min=round(rtts[0], 1),
                rtt_ms_mean=round(sum(rtts) / len(rtts), 1),
                rtt_ms_max=round(rtts[-1], 1),
            )
        return out

    def as_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "first_seen": self.first_seen,
            "messages": self.messages,
            "types": dict(self.types),
            **self._rate_stats(),
            "radio": {mac: s.as_dict() for mac, s in self.rssi.items()},
            "caps": self.caps,
            "echo": self.echo_stats(),
        }
