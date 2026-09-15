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
import statistics
from dataclasses import dataclass

from hometwin.fusion.multilateration import multilaterate
from hometwin.observations import BearingObservation, Observation, PositionObservation, RangeObservation
from hometwin.registry import register
from hometwin.sensors.base import SensorAdapter

ITEM_HEIGHT_PRIOR_M = 0.8
ITEM_HEIGHT_SIGMA_M = 0.6


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


@dataclass
class TagReport:
    """One advertisement heard by one anchor. `tag_id` is the canonical,
    MAC-rotation-proof identity (see `tag_identity`) and matches the strings in
    an item's `tags:` list."""

    anchor_id: str
    tag_id: str
    rssi: float
    timestamp: float
    tx_power: float | None = None  # advertised 1 m reference, when the tag sends it
    azimuth_rad: float | None = None  # BLE 5.1 AoA, anchor frame
    elevation_rad: float | None = None


class AnchorReportSource:
    """Backend contract for a multi-anchor deployment: every TagReport seen
    since the last call, from any anchor."""

    def reports(self) -> list[TagReport]:
        raise NotImplementedError


def tag_identity(
    mac: str,
    ibeacon: tuple[str, int, int] | None = None,
    eddystone_uid: tuple[str, str] | None = None,
) -> str:
    """Canonical tag id. BLE 5.1 asset tags rotate their MAC for privacy, so a
    MAC-keyed track silently splits in two every rotation. Prefer the payload
    identity the tag actually broadcasts and fall back to the MAC only for tags
    that advertise nothing stable."""
    if ibeacon:
        uuid, major, minor = ibeacon
        return f"ble:{uuid.lower()}:{major}:{minor}"
    if eddystone_uid:
        namespace, instance = eddystone_uid
        return f"ble:{namespace.lower()}:{instance.lower()}"
    return f"ble:{mac.upper()}"


def parse_advertisement(mac: str, manufacturer_data: dict | None = None,
                        service_data: dict | None = None) -> tuple[str, float | None]:
    """(tag_id, advertised_tx_power) from a raw advertisement.

    Handles the two formats asset tags actually ship: iBeacon (Apple
    manufacturer data 0x004C, type 0x02) and Eddystone-UID (service 0xFEAA,
    frame 0x00). Unknown payloads degrade to MAC identity.
    """
    md = manufacturer_data or {}
    apple = md.get(0x004C) or md.get("004C") or md.get("4c")
    if apple and len(apple) >= 23 and apple[0] == 0x02 and apple[1] == 0x15:
        uuid = bytes(apple[2:18]).hex()
        major = int.from_bytes(bytes(apple[18:20]), "big")
        minor = int.from_bytes(bytes(apple[20:22]), "big")
        tx = int.from_bytes(bytes(apple[22:23]), "big", signed=True)
        return tag_identity(mac, ibeacon=(uuid, major, minor)), float(tx)

    sd = service_data or {}
    eddy = None
    for key, val in sd.items():
        if "feaa" in str(key).lower():
            eddy = val
            break
    if eddy and len(eddy) >= 18 and eddy[0] == 0x00:
        tx = int.from_bytes(bytes(eddy[1:2]), "big", signed=True)
        namespace = bytes(eddy[2:12]).hex()
        instance = bytes(eddy[12:18]).hex()
        # Eddystone reports power at 0 m; the path-loss model wants 1 m
        return tag_identity(mac, eddystone_uid=(namespace, instance)), float(tx) - 41.0
    return tag_identity(mac), None


class RSSIWindow:
    """Median-then-EWMA smoothing per (anchor, tag).

    Raw BLE RSSI swings 10+ dB between adjacent advertisements from multipath
    and body shadowing, which a path-loss model turns into metres of range
    noise. The median rejects those spikes outright; the EWMA keeps the result
    responsive to real motion.
    """

    def __init__(self, window: int = 7, alpha: float = 0.4):
        self.window = window
        self.alpha = alpha
        self._samples: dict[tuple[str, str], list[float]] = {}
        self._smoothed: dict[tuple[str, str], float] = {}
        self._last_ts: dict[tuple[str, str], float] = {}

    def add(self, anchor_id: str, tag_id: str, rssi: float, ts: float) -> float:
        key = (anchor_id, tag_id)
        buf = self._samples.setdefault(key, [])
        buf.append(rssi)
        if len(buf) > self.window:
            del buf[0]
        med = statistics.median(buf)
        prev = self._smoothed.get(key)
        val = med if prev is None else prev + self.alpha * (med - prev)
        self._smoothed[key] = val
        self._last_ts[key] = ts
        return val

    def value(self, anchor_id: str, tag_id: str) -> tuple[float, int, float] | None:
        key = (anchor_id, tag_id)
        if key not in self._smoothed:
            return None
        return self._smoothed[key], len(self._samples[key]), self._last_ts[key]

    def tags(self) -> set[str]:
        return {tag for _, tag in self._smoothed}

    def anchors_for(self, tag_id: str) -> list[str]:
        return [a for (a, t) in self._smoothed if t == tag_id]

    def drop_stale(self, now: float, max_age_s: float) -> None:
        for key, ts in list(self._last_ts.items()):
            if now - ts > max_age_s:
                self._last_ts.pop(key, None)
                self._smoothed.pop(key, None)
                self._samples.pop(key, None)


class PathLossCalibrator:
    """Fit the radio model from reference tags at known positions.

    tx_power and the path-loss exponent are the dominant error source in RSSI
    ranging and they are properties of the room, not the datasheet — furniture,
    wall material and anchor enclosure all move them. Tags parked at surveyed
    positions give a continuous supply of (rssi, true distance) pairs, so the
    model that locates the *moving* tags is fitted live from the *stationary*
    ones. One shared exponent (the environment) with a per-anchor intercept
    (that anchor's antenna and enclosure) is the ANCOVA form, solved closed-form.
    """

    MIN_SAMPLES = 8
    MIN_SPAN_LOG10 = 0.12  # need distance variety or the slope is unidentifiable

    def __init__(self, default: PathLossModel):
        self.default = default
        self.exponent = default.exponent
        self._acc: dict[str, list[float]] = {}  # anchor -> [n, Sx, Sy, Sxx, Sxy]
        self._tx: dict[str, float] = {}
        self.fitted = False

    def observe(self, anchor_id: str, rssi: float, true_distance_m: float) -> None:
        if true_distance_m <= 0.05:
            return
        x = math.log10(true_distance_m)
        a = self._acc.setdefault(anchor_id, [0.0, 0.0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += x
        a[2] += rssi
        a[3] += x * x
        a[4] += x * rssi
        self._refit()

    def _refit(self) -> None:
        # The exponent is a property of the room, so it is pooled across every
        # anchor that saw enough distance variety to identify a slope. An
        # anchor whose reference tags all sit at one radius cannot do that —
        # but once the room's exponent is known its intercept still follows
        # from the same samples, so it is calibrated too rather than dropped.
        num = den = 0.0
        for n, sx, sy, sxx, sxy in self._acc.values():
            if n < self.MIN_SAMPLES:
                continue
            sxx_c = sxx - sx * sx / n
            if sxx_c <= 0 or math.sqrt(sxx_c / n) < self.MIN_SPAN_LOG10:
                continue
            num += sxy - sx * sy / n
            den += sxx_c
        if den <= 0:
            return
        exponent = min(max(-(num / den) / 10.0, 1.6), 4.5)
        self.exponent = exponent
        for anchor, (n, sx, sy, _, _) in self._acc.items():
            if n < self.MIN_SAMPLES:
                continue
            tx = sy / n + 10.0 * exponent * (sx / n)
            self._tx[anchor] = min(max(tx, -95.0), -30.0)
        self.fitted = True

    def model_for(self, anchor_id: str) -> PathLossModel:
        return PathLossModel(
            self._tx.get(anchor_id, self.default.tx_power),
            self.exponent if self.fitted else self.default.exponent,
        )

    def status(self) -> dict:
        return {
            "fitted": self.fitted,
            "exponent": round(self.exponent, 3),
            "tx_power": {a: round(v, 1) for a, v in self._tx.items()},
            "samples": {a: int(v[0]) for a, v in self._acc.items()},
        }


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
        self.last_rssi: dict[str, tuple[float, float]] = {}  # mac -> (rssi, ts)

    def poll(self) -> list[RangeObservation]:
        if self.source is None:
            return []
        ts = self.clock()
        out = []
        for mac, rssi in self.source.readings():
            self.last_rssi[mac.upper()] = (rssi, ts)
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


def _anchor_spec(spec) -> tuple[tuple[float, float, float], float]:
    """Accept [x,y,z] or {position: [...], yaw_deg: d} for AoA-capable locators."""
    if isinstance(spec, dict):
        pos = tuple(float(v) for v in spec["position"])
        return pos, math.radians(float(spec.get("yaw_deg", 0.0)))
    return tuple(float(v) for v in spec), 0.0


@register("sensor", "ble_mesh")
class BLEMeshSensor(SensorAdapter):
    """Many anchors, one solve: the BLE 5.1 asset-tag tracker.

    A lone scanner can only ever say "about 3 m away" — a ring, not a place.
    This sensor collects what *every* anchor heard for the same tag inside one
    time window and solves them jointly, emitting a real PositionObservation
    with a covariance earned from the geometry. Below the anchor threshold it
    degrades to the per-anchor ranges the fusion engine already understands, so
    a partially-deployed mesh still tracks, just less sharply.

    AoA (BLE 5.1 direction finding) is used when a locator reports it: one
    bearing plus one range fixes a position that RSSI alone cannot.
    """

    def __init__(
        self,
        sensor_id: str,
        anchors: dict | None = None,
        source: AnchorReportSource | None = None,
        tx_power: float = -59.0,
        exponent: float = 2.7,
        reference_tags: dict | None = None,
        window_s: float = 4.0,
        min_anchors: int = 3,
        smoothing: int = 7,
        z_prior: float | None = ITEM_HEIGHT_PRIOR_M,
        z_sigma: float = ITEM_HEIGHT_SIGMA_M,
        aoa_sigma_deg: float = 8.0,
        max_gdop: float = 12.0,
    ):
        super().__init__(sensor_id)
        self.anchors: dict[str, tuple[float, float, float]] = {}
        self.anchor_yaw: dict[str, float] = {}
        for aid, spec in (anchors or {}).items():
            pos, yaw = _anchor_spec(spec)
            self.anchors[aid] = pos
            self.anchor_yaw[aid] = yaw
        self.source = source
        self.model = PathLossModel(tx_power, exponent)
        self.calibrator = PathLossCalibrator(self.model)
        self.reference_tags = {
            str(k): tuple(float(v) for v in pos) for k, pos in (reference_tags or {}).items()
        }
        self.window_s = window_s
        self.min_anchors = max(2, int(min_anchors))
        self.rssi = RSSIWindow(window=smoothing)
        self.z_prior = z_prior
        self.z_sigma = z_sigma
        self.aoa_sigma = math.radians(aoa_sigma_deg)
        self.max_gdop = max_gdop
        self._aoa: dict[tuple[str, str], tuple[float, float, float]] = {}
        self.last_fix: dict[str, dict] = {}

    def attach_anchor(self, anchor_id: str, position, yaw_deg: float = 0.0) -> None:
        self.anchors[anchor_id] = tuple(float(v) for v in position)
        self.anchor_yaw[anchor_id] = math.radians(yaw_deg)

    def poll(self) -> list[Observation]:
        if self.source is None:
            return []
        ts = self.clock()
        for rep in self.source.reports():
            if rep.anchor_id not in self.anchors:
                continue
            smoothed = self.rssi.add(rep.anchor_id, rep.tag_id, rep.rssi, rep.timestamp or ts)
            if rep.azimuth_rad is not None:
                self._aoa[(rep.anchor_id, rep.tag_id)] = (
                    rep.azimuth_rad,
                    rep.elevation_rad or 0.0,
                    rep.timestamp or ts,
                )
            ref = self.reference_tags.get(rep.tag_id)
            if ref is not None:
                self.calibrator.observe(
                    rep.anchor_id, smoothed, math.dist(self.anchors[rep.anchor_id], ref)
                )
        self.rssi.drop_stale(ts, self.window_s * 3)

        out: list[Observation] = []
        for tag in sorted(self.rssi.tags()):
            if tag in self.reference_tags:
                continue  # infrastructure, not an item
            out.extend(self._observe_tag(tag, ts))
        return out

    def _ranges_for(self, tag: str, ts: float):
        anchors, ranges, sigmas, ids = [], [], [], []
        for aid in sorted(self.rssi.anchors_for(tag)):
            got = self.rssi.value(aid, tag)
            if got is None:
                continue
            rssi, samples, last = got
            if ts - last > self.window_s:
                continue
            model = self.calibrator.model_for(aid)
            d = model.rssi_to_range(rssi)
            # smoothing averages down jitter but not multipath bias, so only
            # part of the 1/sqrt(n) improvement is real
            sigma = model.range_sigma(d) * max(0.6, 1.0 / math.sqrt(max(samples, 1)))
            anchors.append(self.anchors[aid])
            ranges.append(d)
            sigmas.append(sigma)
            ids.append(aid)
        return anchors, ranges, sigmas, ids

    def _observe_tag(self, tag: str, ts: float) -> list[Observation]:
        anchors, ranges, sigmas, ids = self._ranges_for(tag, ts)
        out: list[Observation] = []
        out.extend(self._bearings_for(tag, ts))
        if not anchors:
            return out

        if len(anchors) >= self.min_anchors:
            fix = multilaterate(
                anchors,
                ranges,
                sigmas,
                z_prior=self.z_prior,
                z_sigma=self.z_sigma,
                max_gdop=self.max_gdop,
            )
            if fix is not None:
                self.last_fix[tag] = {
                    "position": [round(v, 3) for v in fix.position],
                    "sigma_m": round(fix.sigma_m, 3),
                    "gdop": round(fix.gdop, 2),
                    "residual_rms_m": round(fix.residual_rms_m, 3),
                    "anchors": [ids[i] for i in fix.used],
                    "rejected": [ids[i] for i in fix.dropped],
                    "timestamp": ts,
                }
                out.append(
                    PositionObservation(
                        sensor_id=self.sensor_id,
                        timestamp=ts,
                        item_id=tag,
                        position=fix.position,
                        sigma_m=fix.sigma_m,
                    )
                )
                return out
        # too few anchors, or geometry the solver refused: hand the engine the
        # raw ranges and let the filter do what it can with them
        for anchor, d, sigma in zip(anchors, ranges, sigmas):
            out.append(
                RangeObservation(
                    sensor_id=self.sensor_id,
                    timestamp=ts,
                    item_id=tag,
                    anchor=anchor,
                    range_m=d,
                    sigma_m=sigma,
                )
            )
        return out

    def _bearings_for(self, tag: str, ts: float) -> list[Observation]:
        out = []
        for (aid, tid), (az, el, last) in list(self._aoa.items()):
            if tid != tag:
                continue
            if ts - last > self.window_s:
                del self._aoa[(aid, tid)]
                continue
            yaw = self.anchor_yaw.get(aid, 0.0)
            a = az + yaw
            direction = (math.cos(el) * math.cos(a), math.cos(el) * math.sin(a), math.sin(el))
            out.append(
                BearingObservation(
                    sensor_id=self.sensor_id,
                    timestamp=ts,
                    item_id=tag,
                    origin=self.anchors[aid],
                    direction=direction,
                    sigma_rad=self.aoa_sigma,
                )
            )
        return out

    def calibration_status(self) -> dict:
        return {"sensor_id": self.sensor_id, **self.calibrator.status()}


class QueueReportSource(AnchorReportSource):
    """Push-fed source. Any transport that already receives anchor scans — the
    network bridge, MQTT, ESP-NOW — calls `push()`; the mesh drains on poll."""

    def __init__(self, max_pending: int = 4096):
        self._pending: list[TagReport] = []
        self.max_pending = max_pending

    def push(self, report: TagReport) -> None:
        self._pending.append(report)
        if len(self._pending) > self.max_pending:
            del self._pending[: len(self._pending) - self.max_pending]

    def push_rssi(self, anchor_id: str, tag_id: str, rssi: float, ts: float, **kw) -> None:
        self.push(TagReport(anchor_id=anchor_id, tag_id=tag_id, rssi=rssi, timestamp=ts, **kw))

    def reports(self) -> list[TagReport]:
        out, self._pending = self._pending, []
        return out


class BleakSource(AnchorReportSource):
    """Host Bluetooth adapter as a single anchor, via bleak.

    One host is one anchor: useful for a laptop demo or as the anchor of last
    resort, but a single anchor can only ever produce a range ring. Triangulation
    needs three, which in practice means ESP32 nodes feeding QueueReportSource.
    """

    def __init__(self, anchor_id: str, adapter: str | None = None):
        self.anchor_id = anchor_id
        self.adapter = adapter
        self._queue = QueueReportSource()
        self._scanner = None
        self._loop = None
        self._thread = None

    def start(self) -> None:
        import asyncio
        import threading
        import time

        from bleak import BleakScanner

        def on_detect(device, adv):
            tag_id, tx = parse_advertisement(
                device.address,
                dict(getattr(adv, "manufacturer_data", {}) or {}),
                dict(getattr(adv, "service_data", {}) or {}),
            )
            self._queue.push_rssi(
                self.anchor_id, tag_id, float(adv.rssi), time.time(), tx_power=tx
            )

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            kw = {"adapter": self.adapter} if self.adapter else {}
            self._scanner = BleakScanner(detection_callback=on_detect, **kw)
            self._loop.run_until_complete(self._scanner.start())
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="ble-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def reports(self) -> list[TagReport]:
        return self._queue.reports()
