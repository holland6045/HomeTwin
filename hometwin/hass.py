"""Home Assistant exposure: detection zones as synthetic motion sensors.

Each configured `motion_zones` entry is a region of the map (box or
circle) that behaves like a physical motion sensor: presence evidence
inside it — RF-tomography blobs, door/drawer activity, any future
presence-class signal — turns it ON immediately; it turns OFF after
`off_delay_s` without evidence (the same semantics PIR integrations use).

Zones surface three ways:
- Home Assistant via MQTT discovery: each zone registers as its own
  device with a motion `binary_sensor` entity, so automations treat them
  exactly like hardware PIRs;
- the dashboard map (state-colored regions; shift-drag to draw new ones);
- the event feed and `/overlay/map -> motion_zones`.

The MQTT client is a deliberately minimal publish-only stdlib
implementation (CONNECT/CONNACK/PUBLISH/PING, QoS 0): HomeTwin only ever
pushes states, and the core stays dependency-free. Broker outages are
absorbed — publishing retries with backoff and never touches tracking.
"""

from __future__ import annotations

import json
import logging
import math
import re
import socket
import struct
import threading
import time

log = logging.getLogger("hometwin")

DEFAULT_OFF_DELAY_S = 30.0
SIGMA_MARGIN = 0.5  # presence blobs count if within sigma*this of the zone


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class MotionZone:
    def __init__(
        self,
        name: str,
        min_corner=None,
        max_corner=None,
        center=None,
        radius: float | None = None,
        off_delay_s: float = DEFAULT_OFF_DELAY_S,
    ):
        if (min_corner is None or max_corner is None) and (center is None or radius is None):
            raise ValueError(f"motion zone {name!r} needs min/max or center+radius")
        self.name = name
        self.slug = slugify(name)
        self.min = tuple(min_corner) if min_corner else None
        self.max = tuple(max_corner) if max_corner else None
        self.center = tuple(center) if center else None
        self.radius = float(radius) if radius is not None else None
        self.off_delay_s = off_delay_s
        self.on = False
        self.last_detect = 0.0

    def _distance_outside(self, p) -> float:
        """0 inside the zone; otherwise distance to it (xy; z ignored for
        circles, box z used only when the box constrains it)."""
        if self.center is not None:
            return max(0.0, math.hypot(p[0] - self.center[0], p[1] - self.center[1]) - self.radius)
        d = 0.0
        for i in range(3):
            lo, hi = self.min[i], self.max[i]
            if p[i] < lo:
                d += (lo - p[i]) ** 2
            elif p[i] > hi:
                d += (p[i] - hi) ** 2
        return math.sqrt(d)

    def hit(self, p, sigma_m: float = 0.0) -> bool:
        return self._distance_outside(p) <= sigma_m * SIGMA_MARGIN

    def geometry(self) -> dict:
        if self.center is not None:
            return {"center": list(self.center), "radius": self.radius}
        return {"min": list(self.min), "max": list(self.max)}


class MotionZoneController:
    """Folds presence evidence into per-zone motion state; emits changes."""

    def __init__(self, zones: list[MotionZone] | None = None):
        self.zones: list[MotionZone] = list(zones or [])
        self._lock = threading.Lock()

    def add(self, zone: MotionZone) -> None:
        with self._lock:
            if any(z.slug == zone.slug for z in self.zones):
                raise ValueError(f"motion zone {zone.name!r} already exists")
            self.zones.append(zone)

    def evidence(self, p, ts: float, sigma_m: float = 0.0) -> list[MotionZone]:
        """A presence-class detection at p: returns zones newly turned ON."""
        turned_on = []
        with self._lock:
            for z in self.zones:
                if z.hit(p, sigma_m):
                    z.last_detect = ts
                    if not z.on:
                        z.on = True
                        turned_on.append(z)
        return turned_on

    def expire(self, now: float) -> list[MotionZone]:
        """Returns zones newly turned OFF after their delay."""
        turned_off = []
        with self._lock:
            for z in self.zones:
                if z.on and now - z.last_detect > z.off_delay_s:
                    z.on = False
                    turned_off.append(z)
        return turned_off

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "name": z.name,
                    "slug": z.slug,
                    "on": z.on,
                    "last_detect": z.last_detect,
                    "off_delay_s": z.off_delay_s,
                    **z.geometry(),
                }
                for z in self.zones
            ]

    @classmethod
    def from_config(cls, cfgs: list[dict], default_off_delay: float) -> "MotionZoneController":
        return cls([
            MotionZone(
                c["name"],
                min_corner=c.get("min"),
                max_corner=c.get("max"),
                center=c.get("center"),
                radius=c.get("radius"),
                off_delay_s=float(c.get("off_delay_s", default_off_delay)),
            )
            for c in cfgs
        ])


def _encode_remaining(length: int) -> bytes:
    out = b""
    while True:
        byte = length % 128
        length //= 128
        out += bytes([byte | (0x80 if length else 0)])
        if not length:
            return out


def _mqtt_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack(">H", len(b)) + b


class MiniMqtt:
    """Publish-only MQTT 3.1.1 client, QoS 0. Reconnects with backoff;
    failures are logged and swallowed — the broker must never hurt
    tracking."""

    def __init__(self, host: str, port: int = 1883, username: str | None = None,
                 password: str | None = None, client_id: str = "hometwin"):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.client_id = client_id
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._next_retry = 0.0

    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        flags = 0x02  # clean session
        payload = _mqtt_string(self.client_id)
        if self.username is not None:
            flags |= 0x80
            payload += _mqtt_string(self.username)
            if self.password is not None:
                flags |= 0x40
                payload += _mqtt_string(self.password)
        var = _mqtt_string("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
        packet = bytes([0x10]) + _encode_remaining(len(var) + len(payload)) + var + payload
        sock.sendall(packet)
        connack = sock.recv(4)
        if len(connack) < 4 or connack[0] != 0x20 or connack[3] != 0:
            sock.close()
            raise ConnectionError(f"MQTT CONNACK refused: {connack.hex() if connack else 'empty'}")
        self._sock = sock

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._sock is None:
                if now < self._next_retry:
                    return False
                try:
                    self._connect()
                except OSError as e:
                    self._next_retry = now + 15.0
                    log.warning("MQTT connect to %s:%s failed: %s", self.host, self.port, e)
                    return False
            body = payload.encode("utf-8")
            var = _mqtt_string(topic)
            packet = (bytes([0x30 | (0x01 if retain else 0x00)])
                      + _encode_remaining(len(var) + len(body)) + var + body)
            try:
                self._sock.sendall(packet)
                return True
            except OSError as e:
                log.warning("MQTT publish failed (%s); will reconnect", e)
                try:
                    self._sock.close()
                finally:
                    self._sock = None
                    self._next_retry = now + 5.0
                return False

    def close(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.sendall(bytes([0xE0, 0x00]))  # DISCONNECT
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None


class HomeAssistantBridge:
    """Registers each motion zone as its own HA device via MQTT discovery
    and pushes state transitions."""

    def __init__(self, mqtt: MiniMqtt, discovery_prefix: str = "homeassistant"):
        self.mqtt = mqtt
        self.prefix = discovery_prefix
        self._announced: set[str] = set()

    def _state_topic(self, zone: MotionZone) -> str:
        return f"hometwin/motion/{zone.slug}/state"

    def announce(self, zone: MotionZone) -> None:
        if zone.slug in self._announced:
            return
        config = {
            "name": zone.name,
            "unique_id": f"hometwin_motion_{zone.slug}",
            "device_class": "motion",
            "state_topic": self._state_topic(zone),
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": "hometwin/availability",
            "device": {
                "identifiers": [f"hometwin_zone_{zone.slug}"],
                "name": f"HomeTwin {zone.name}",
                "manufacturer": "HomeTwin",
                "model": "synthetic presence zone",
            },
        }
        topic = f"{self.prefix}/binary_sensor/hometwin_{zone.slug}/config"
        if self.mqtt.publish(topic, json.dumps(config), retain=True):
            self.mqtt.publish("hometwin/availability", "online", retain=True)
            self.mqtt.publish(self._state_topic(zone), "ON" if zone.on else "OFF",
                              retain=True)
            self._announced.add(zone.slug)

    def publish_state(self, zone: MotionZone) -> None:
        self.announce(zone)
        self.mqtt.publish(self._state_topic(zone), "ON" if zone.on else "OFF", retain=True)

    def announce_all(self, controller: MotionZoneController) -> None:
        for zone in list(controller.zones):
            self.announce(zone)

    def close(self) -> None:
        self.mqtt.publish("hometwin/availability", "offline", retain=True)
        self.mqtt.close()
