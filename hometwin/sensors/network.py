"""Network bridge: hardware-agnostic ingest from MCU nodes.

Any device that can open a TCP socket and print JSON lines participates —
ESP32, RP2040 W, a phone, another process. One line per message:

  {"type": "position", "sensor_id": "cam-2", "item": "aruco:7",
   "pos": [1.2, 3.4, 0.9], "sigma_m": 0.3}
  {"type": "range", "sensor_id": "esp32-hall", "item": "ble:AA:BB:CC:DD:EE:FF",
   "anchor": [0.0, 4.0, 2.2], "range_m": 2.1, "sigma_m": 0.9}
  {"type": "rssi", "sensor_id": "esp32-hall", "mac": "AA:BB:CC:DD:EE:FF",
   "anchor": [0.0, 4.0, 2.2], "rssi": -67}
  {"type": "bearing", "sensor_id": "esp32-cam-2", "item": "aruco:7",
   "origin": [0.2, 0.2, 2.4], "direction": [0.6, 0.6, -0.5], "sigma_rad": 0.03}
  {"type": "area", "sensor_id": "rti-mesh", "label": "presence",
   "centroid": [2.0, 2.0, 1.0], "sigma_m": 1.5}

Timestamps are assigned on receipt; MCU clocks are never trusted. Malformed
lines are dropped and counted, never fatal — a flaky node must not take the
tracker down.

Security: with `auth_token` set, the first line of every connection must be
{"auth": "<token>"} or the connection is closed — one constant-time check
per connection, zero per-message cost (ESP32-friendly). Lines are capped at
64 KiB so a misbehaving peer cannot balloon memory. The transport is plain
TCP: run it on a trusted/segmented IoT network; for hostile networks put
the nodes behind a WireGuard/TLS tunnel rather than per-message crypto.
"""

from __future__ import annotations

import hmac
import json
import socketserver
import threading
import time
from collections import deque

MAX_LINE = 64 * 1024

from hometwin.observations import (
    AreaObservation,
    BearingObservation,
    Observation,
    PositionObservation,
    RangeObservation,
)
from hometwin.registry import register
from hometwin.sensors.base import SensorAdapter
from hometwin.sensors.ble import PathLossModel


def parse_message(msg: dict, default_sensor: str, model: PathLossModel) -> Observation | None:
    kind = msg.get("type")
    sensor_id = msg.get("sensor_id", default_sensor)
    ts = time.time()
    common = dict(
        sensor_id=sensor_id,
        timestamp=ts,
        item_id=msg.get("item"),
        label=msg.get("label"),
        confidence=float(msg.get("confidence", 1.0)),
    )
    if kind == "position":
        return PositionObservation(
            **common, position=tuple(msg["pos"]), sigma_m=float(msg.get("sigma_m", 0.3))
        )
    if kind == "range":
        return RangeObservation(
            **common,
            anchor=tuple(msg["anchor"]),
            range_m=float(msg["range_m"]),
            sigma_m=float(msg.get("sigma_m", 1.0)),
        )
    if kind == "rssi":
        d = model.rssi_to_range(float(msg["rssi"]))
        common["item_id"] = common["item_id"] or f"ble:{msg['mac'].upper()}"
        return RangeObservation(
            **common, anchor=tuple(msg["anchor"]), range_m=d, sigma_m=model.range_sigma(d)
        )
    if kind == "bearing":
        d = msg["direction"]
        n = (d[0] ** 2 + d[1] ** 2 + d[2] ** 2) ** 0.5
        if n < 1e-9:
            return None
        return BearingObservation(
            **common,
            origin=tuple(msg["origin"]),
            direction=(d[0] / n, d[1] / n, d[2] / n),
            sigma_rad=float(msg.get("sigma_rad", 0.02)),
        )
    if kind == "area":
        return AreaObservation(
            **common, centroid=tuple(msg["centroid"]), sigma_m=float(msg.get("sigma_m", 2.0))
        )
    return None


@register("sensor", "network_bridge")
class NetworkBridgeSensor(SensorAdapter):
    def __init__(
        self,
        sensor_id: str = "bridge",
        host: str = "0.0.0.0",
        port: int = 8787,
        tx_power: float = -59.0,
        exponent: float = 2.7,
        max_queue: int = 10000,
        auth_token: str | None = None,
    ):
        super().__init__(sensor_id)
        self.host, self.port = host, port
        self.model = PathLossModel(tx_power, exponent)
        self.auth_token = auth_token
        self._queue: deque[Observation] = deque(maxlen=max_queue)
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self.rejected_connections = 0

    def check_auth(self, line: str) -> bool:
        try:
            presented = json.loads(line).get("auth", "")
        except (json.JSONDecodeError, AttributeError):
            return False
        return isinstance(presented, str) and hmac.compare_digest(
            self.auth_token, presented
        )

    def handle_line(self, line: str) -> None:
        try:
            obs = parse_message(json.loads(line), self.sensor_id, self.model)
        except (ValueError, KeyError, TypeError):
            self.dropped += 1
            return
        if obs is None:
            self.dropped += 1
            return
        with self._lock:
            self._queue.append(obs)

    def start(self) -> None:
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                authed = bridge.auth_token is None
                while True:
                    raw = self.rfile.readline(MAX_LINE + 1)
                    if not raw:
                        return
                    if len(raw) > MAX_LINE:
                        bridge.dropped += 1
                        if not authed:
                            return  # oversized pre-auth garbage: hang up
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if not authed:
                        if not bridge.check_auth(line):
                            bridge.rejected_connections += 1
                            return
                        authed = True
                        continue
                    bridge.handle_line(line)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]  # resolves port 0 -> real port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def poll(self) -> list[Observation]:
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out
