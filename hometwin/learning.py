"""Online self-improvement: tracking paths that calibrate themselves.

PathLossLearner closes the BLE loop without any user action: whenever an
item carrying a BLE tag has a camera-confirmed position (a strong fix in
the last few seconds), the scanner's raw RSSI for that tag plus the fused
distance form a free labeled sample. Enough samples with enough distance
spread re-fit the scanner's path-loss model (same closed form as the
offline trainer), blended in gently and clamped to physical bounds — BLE
ranging accuracy improves over time as the system simply gets used.
"""

from __future__ import annotations

import math
from collections import deque

SAMPLE_WINDOW = 200
REFIT_EVERY = 25
MIN_SAMPLES = 30
MIN_LOG_SPREAD = 0.3  # distances must span >= 2x before a fit is trusted
BLEND = 0.3
TX_BOUNDS = (-80.0, -40.0)
EXP_BOUNDS = (1.5, 4.5)
FRESH_FIX_S = 5.0
MAX_TRACK_SIGMA_M = 0.3


def fit_path_loss(samples) -> tuple[float, float] | None:
    """Closed-form fit of (tx_power, exponent) from (dist_m, rssi) pairs."""
    pts = [(math.log10(d), r) for d, r in samples if d > 0.05]
    n = len(pts)
    if n < 2:
        return None
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx < 1e-9:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return (my - slope * mx, -slope / 10.0)


class PathLossLearner:
    def __init__(self, scanner):
        self.scanner = scanner
        self.samples: deque[tuple[float, float]] = deque(maxlen=SAMPLE_WINDOW)
        self._since_fit = 0
        self.refits = 0

    def add_sample(self, dist_m: float, rssi: float) -> None:
        self.samples.append((dist_m, rssi))
        self._since_fit += 1
        if self._since_fit >= REFIT_EVERY and len(self.samples) >= MIN_SAMPLES:
            self._refit()

    def _refit(self) -> None:
        logs = [math.log10(d) for d, _ in self.samples if d > 0.05]
        if not logs or max(logs) - min(logs) < MIN_LOG_SPREAD:
            return  # all samples at one distance teach nothing
        fit = fit_path_loss(self.samples)
        if fit is None:
            return
        tx, exp = fit
        model = self.scanner.model
        model.tx_power = min(max(
            (1 - BLEND) * model.tx_power + BLEND * tx, TX_BOUNDS[0]), TX_BOUNDS[1])
        model.exponent = min(max(
            (1 - BLEND) * model.exponent + BLEND * exp, EXP_BOUNDS[0]), EXP_BOUNDS[1])
        self._since_fit = 0
        self.refits += 1

    def status(self) -> dict:
        return {
            "sensor_id": self.scanner.sensor_id,
            "samples": len(self.samples),
            "refits": self.refits,
            "tx_power": round(self.scanner.model.tx_power, 2),
            "exponent": round(self.scanner.model.exponent, 3),
        }


def collect_ble_samples(tracker) -> None:
    """Harvest (camera-confirmed distance, raw RSSI) pairs from every BLE
    scanner. Called by the tracker each step; costs a few dict lookups.

    Gate: the item's track must have a fresh strong (position/bearing) fix
    and tight covariance — the scanner must never teach itself from
    positions its own ranges produced.
    """
    learners = getattr(tracker, "_path_loss_learners", None)
    if learners is None:
        learners = tracker._path_loss_learners = {}
    now_tracks = tracker.engine.tracks
    for sensor in tracker.sensors:
        last_rssi = getattr(sensor, "last_rssi", None)
        if last_rssi is None or not hasattr(sensor, "model"):
            continue
        learner = learners.get(sensor.sensor_id)
        if learner is None:
            learner = learners[sensor.sensor_id] = PathLossLearner(sensor)
        for mac, (rssi, ts) in list(last_rssi.items()):
            item_id = tracker.cfg.items.resolve_tag(f"ble:{mac.upper()}")
            track = now_tracks.get(item_id) if item_id else None
            if track is None or track.sigma_m > MAX_TRACK_SIGMA_M:
                continue
            if ts - track.last_strong_update > FRESH_FIX_S:
                continue  # position not currently camera/bearing-confirmed
            dist = math.dist(track.position, sensor.position)
            learner.add_sample(dist, rssi)
            del last_rssi[mac]  # one sample per reading
