"""Network bridge: hardware-agnostic ingest from MCU nodes.

Any device that can open a TCP socket and print JSON lines participates —
ESP32, RP2040 W, a phone, another process. One line per message:

  {"type": "position", "sensor_id": "cam-2", "item": "aruco:7",
   "pos": [1.2, 3.4, 0.9], "sigma_m": 0.3}
  {"type": "range", "sensor_id": "esp32-hall", "item": "ble:AA:BB:CC:DD:EE:FF",
   "anchor": [0.0, 4.0, 2.2], "range_m": 2.1, "sigma_m": 0.9}
  {"type": "rssi", "sensor_id": "esp32-hall", "mac": "AA:BB:CC:DD:EE:FF",
   "anchor": [0.0, 4.0, 2.2], "rssi": -67}
  {"type": "area", "sensor_id": "rti-mesh", "label": "presence",
   "centroid": [2.0, 2.0, 1.0], "sigma_m": 1.5}

Timestamps are assigned on receipt; MCU clocks are never trusted. Malformed
lines are dropped and counted, never fatal — a flaky node must not take the
tracker down.
"""

from __future__ import annotations

import json
import socketserver
import threading
import time
from collections import deque

from apartment_tracker.observations import (
    AreaObservation,
    Observation,
    PositionObservation,
    RangeObservation,
)
from apartment_tracker.registry import register
from apartment_tracker.sensors.base import SensorAdapter
from apartment_tracker.sensors.ble import PathLossModel


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
    ):
        super().__init__(sensor_id)
        self.host, self.port = host, port
        self.model = PathLossModel(tx_power, exponent)
        self._queue: deque[Observation] = deque(maxlen=max_queue)
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None
        self.dropped = 0

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
                for raw in self.rfile:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
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
