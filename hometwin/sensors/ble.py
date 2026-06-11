"""BLE beacon ranging: RSSI -> range observation via a log-distance path-loss
model. Works with any tag that advertises (item trackers, cheap nRF beacons)
and any scanner that can report (mac, rssi) pairs — a host adapter via bleak,
or ESP32 nodes forwarding scans over the network bridge.

rssi(d) = tx_power_at_1m - 10 * n * log10(d)

`tx_power` and `n` are per-environment and trainable from labeled samples
(see training/trainers.py PathLossTrainer).
"""

from __future__ import annotations

import math
import time

from hometwin.observations import RangeObservation
from hometwin.registry import register
from hometwin.sensors.base import SensorAdapter


class PathLossModel:
    def __init__(self, tx_power: float = -59.0, exponent: float = 2.7):
        self.tx_power = tx_power
        self.exponent = exponent

    def rssi_to_range(self, rssi: float) -> float:
        return 10.0 ** ((self.tx_power - rssi) / (10.0 * self.exponent))

    def range_to_rssi(self, d: float) -> float:
        return self.tx_power - 10.0 * self.exponent * math.log10(max(d, 1e-3))

    def range_sigma(self, d: float) -> float:
        # multipath error scales roughly with distance; floor at 0.5 m
        return max(0.5, 0.35 * d)


class RSSISource:
    """Backend contract: return (tag_mac, rssi) pairs seen since last call."""

    def readings(self) -> list[tuple[str, float]]:
        raise NotImplementedError


@register("sensor", "ble_scanner")
class BLEScannerSensor(SensorAdapter):
    """One scanner at a known position; emits a RangeObservation per tag seen."""

    def __init__(
        self,
        sensor_id: str,
        position: tuple[float, float, float],
        source: RSSISource | None = None,
        tx_power: float = -59.0,
        exponent: float = 2.7,
    ):
        super().__init__(sensor_id)
        self.position = tuple(position)
        self.source = source
        self.model = PathLossModel(tx_power, exponent)

    def poll(self) -> list[RangeObservation]:
        if self.source is None:
            return []
        ts = self.clock()
        out = []
        for mac, rssi in self.source.readings():
            d = self.model.rssi_to_range(rssi)
            out.append(
                RangeObservation(
                    sensor_id=self.sensor_id,
                    timestamp=ts,
                    item_id=f"ble:{mac.upper()}",
                    anchor=self.position,
                    range_m=d,
                    sigma_m=self.model.range_sigma(d),
                )
            )
        return out
