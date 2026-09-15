"""BLE 5.1 asset tags: multi-anchor triangulation, reference-tag calibration,
tag identity, and AoA."""

import math
import random

import pytest

from hometwin.fusion.multilateration import multilaterate
from hometwin.sensors.ble import (
    BLEMeshSensor,
    PathLossCalibrator,
    PathLossModel,
    RSSIWindow,
    TagReport,
    parse_advertisement,
    tag_identity,
)

CEILING = {
    "a1": (0.0, 0.0, 2.4),
    "a2": (5.0, 0.0, 2.4),
    "a3": (5.0, 4.0, 2.4),
    "a4": (0.0, 4.0, 2.4),
}
TRUE = (3.2, 1.4, 0.8)


def test_multilateration_recovers_exact_ranges():
    anchors = list(CEILING.values())
    ranges = [math.dist(a, TRUE) for a in anchors]
    fix = multilaterate(anchors, ranges, [0.5] * 4, z_prior=0.8)
    assert fix is not None
    for got, want in zip(fix.position, TRUE):
        assert got == pytest.approx(want, abs=0.01)
    assert fix.residual_rms_m < 0.01
    assert fix.anchors_used == 4


def test_multilateration_refuses_degenerate_geometry():
    collinear = [(0.0, 0.0, 2.4), (2.0, 0.0, 2.4), (4.0, 0.0, 2.4)]
    ranges = [math.dist(a, (2.0, 3.0, 0.8)) for a in collinear]
    assert multilaterate(collinear, ranges, [0.8] * 3, z_prior=0.8) is None
    # two ranges leave a mirror ambiguity; better to decline than guess
    two = list(CEILING.values())[:2]
    assert multilaterate(two, [math.dist(a, TRUE) for a in two], [0.8] * 2, z_prior=0.8) is None


def test_multilateration_rejects_one_nlos_anchor():
    """A blocked anchor over-reports distance. Least squares hides it by
    spreading the error, so the solver must find it by leave-one-out."""
    anchors = list(CEILING.values())
    ranges = [math.dist(a, TRUE) for a in anchors]
    ranges[2] += 4.0
    fix = multilaterate(anchors, ranges, [0.8] * 4, z_prior=0.8)
    assert fix is not None
    assert fix.dropped == [2]
    assert math.dist(fix.position, TRUE) < 0.15


def test_multilateration_keeps_honest_anchors():
    random.seed(11)
    kept = 0
    for _ in range(60):
        ranges = [math.dist(a, TRUE) + random.gauss(0, 0.6) for a in CEILING.values()]
        fix = multilaterate(list(CEILING.values()), ranges, [0.6] * 4, z_prior=0.8)
        if fix and not fix.dropped:
            kept += 1
    assert kept > 48  # noise alone must not trigger rejection


class FakeMesh:
    """Anchors hearing a tag through a known path-loss model."""

    def __init__(self, anchors, model, truth, noise_db=0.0, seed=1, only=None):
        self.anchors, self.model, self.truth = anchors, model, truth
        self.noise, self.rng = noise_db, random.Random(seed)
        self.only = only

    def reports(self):
        import time

        self.t = time.time()  # real sensors stamp wall time; staleness relies on it
        out = []
        for aid, pos in self.anchors.items():
            if self.only and aid not in self.only:
                continue
            for tag, truth in self.truth.items():
                d = math.dist(pos, truth)
                rssi = self.model.range_to_rssi(d) + self.rng.gauss(0, self.noise)
                out.append(TagReport(anchor_id=aid, tag_id=tag, rssi=rssi, timestamp=self.t))
        return out


def _mesh(**kw):
    return BLEMeshSensor("ble-mesh", anchors=CEILING, **kw)


def test_mesh_triangulates_position_from_four_anchors():
    from hometwin.observations import PositionObservation

    model = PathLossModel(-59.0, 2.7)
    sensor = _mesh(tx_power=-59.0, exponent=2.7)
    sensor.source = FakeMesh(CEILING, model, {"ble:TAG1": TRUE}, noise_db=2.0)
    for _ in range(8):
        obs = sensor.poll()
    fixes = [o for o in obs if isinstance(o, PositionObservation)]
    assert len(fixes) == 1
    assert fixes[0].item_id == "ble:TAG1"
    assert math.dist(fixes[0].position, TRUE) < 1.2
    assert 0.0 < fixes[0].sigma_m < 3.0
    assert sensor.last_fix["ble:TAG1"]["anchors"]


def test_mesh_degrades_to_ranges_below_anchor_threshold():
    from hometwin.observations import PositionObservation, RangeObservation

    model = PathLossModel(-59.0, 2.7)
    sensor = _mesh()
    sensor.source = FakeMesh(CEILING, model, {"ble:TAG1": TRUE}, only={"a1", "a2"})
    for _ in range(4):
        obs = sensor.poll()
    assert not [o for o in obs if isinstance(o, PositionObservation)]
    ranges = [o for o in obs if isinstance(o, RangeObservation)]
    assert len(ranges) == 2
    assert all(o.item_id == "ble:TAG1" for o in ranges)


def test_triangulation_beats_single_anchor_ranging():
    """The point of the mesh: a joint solve localizes, a lone range only says
    'somewhere on this sphere'."""
    model = PathLossModel(-59.0, 2.7)
    sensor = _mesh()
    sensor.source = FakeMesh(CEILING, model, {"ble:TAG1": TRUE}, noise_db=3.0, seed=5)
    for _ in range(10):
        obs = sensor.poll()
    fix = obs[0]
    # a single anchor's range error alone is the radius uncertainty
    lone = model.rssi_to_range(model.range_to_rssi(math.dist(CEILING["a1"], TRUE)))
    assert math.dist(fix.position, TRUE) < max(1.5, 0.35 * lone)


def test_reference_tags_calibrate_the_radio_model():
    """Stationary tags at known spots teach the model that locates the moving
    ones — the room's exponent, not the datasheet's."""
    room = PathLossModel(tx_power=-66.0, exponent=3.4)  # not the configured default
    refs = {"ble:REF1": (0.5, 0.5, 1.0), "ble:REF2": (4.5, 3.5, 1.0), "ble:REF3": (2.5, 2.0, 0.4)}
    sensor = _mesh(tx_power=-59.0, exponent=2.7, reference_tags=refs)
    truth = dict(refs)
    truth["ble:TAG1"] = TRUE
    sensor.source = FakeMesh(CEILING, room, truth, noise_db=1.0, seed=3)
    for _ in range(30):
        obs = sensor.poll()

    assert sensor.calibrator.fitted
    assert sensor.calibrator.exponent == pytest.approx(3.4, abs=0.35)
    for anchor in CEILING:
        assert sensor.calibrator.model_for(anchor).tx_power == pytest.approx(-66.0, abs=3.0)
    # reference tags are infrastructure, never emitted as items
    assert {o.item_id for o in obs} == {"ble:TAG1"}
    assert math.dist(obs[0].position, TRUE) < 1.5


def test_uncalibrated_model_is_worse_than_calibrated():
    room = PathLossModel(tx_power=-66.0, exponent=3.4)
    refs = {"ble:REF1": (0.5, 0.5, 1.0), "ble:REF2": (4.5, 3.5, 1.0), "ble:REF3": (2.5, 2.0, 0.4)}
    truth = dict(refs, **{"ble:TAG1": TRUE})

    blind = _mesh(tx_power=-59.0, exponent=2.7)
    blind.source = FakeMesh(CEILING, room, truth, noise_db=1.0, seed=3)
    for _ in range(30):
        blind_obs = blind.poll()

    tuned = _mesh(tx_power=-59.0, exponent=2.7, reference_tags=refs)
    tuned.source = FakeMesh(CEILING, room, truth, noise_db=1.0, seed=3)
    for _ in range(30):
        tuned_obs = tuned.poll()

    blind_err = math.dist([o for o in blind_obs if o.item_id == "ble:TAG1"][0].position, TRUE)
    tuned_err = math.dist([o for o in tuned_obs if o.item_id == "ble:TAG1"][0].position, TRUE)
    assert tuned_err < blind_err


def test_rssi_window_rejects_multipath_spikes():
    w = RSSIWindow(window=7, alpha=0.4)
    for _ in range(7):
        w.add("a1", "t1", -70.0, 0.0)
    steady, _, _ = w.value("a1", "t1")
    w.add("a1", "t1", -40.0, 1.0)  # one reflection
    spiked, _, _ = w.value("a1", "t1")
    assert abs(spiked - steady) < 2.0


def test_tag_identity_survives_mac_rotation():
    """BLE 5.1 tags randomize their MAC; a MAC-keyed track would split in two."""
    ibeacon = bytes([0x02, 0x15]) + bytes.fromhex("f" * 32) + b"\x00\x07" + b"\x00\x2a" + b"\xc5"
    first, tx = parse_advertisement("AA:BB:CC:DD:EE:01", {0x004C: ibeacon})
    second, _ = parse_advertisement("11:22:33:44:55:66", {0x004C: ibeacon})
    assert first == second == "ble:" + "f" * 32 + ":7:42"
    assert tx == -59.0

    eddy = bytes([0x00, 0xEE]) + bytes.fromhex("aa" * 10) + bytes.fromhex("bb" * 6)
    uid, _ = parse_advertisement("AA:BB:CC:DD:EE:01", None, {"0000feaa-0000-1000-8000-00805f9b34fb": eddy})
    assert uid == "ble:" + "aa" * 10 + ":" + "bb" * 6
    assert parse_advertisement("AA:BB:CC:DD:EE:01")[0] == tag_identity("aa:bb:cc:dd:ee:01")


def test_aoa_report_becomes_a_world_frame_bearing():
    from hometwin.observations import BearingObservation

    sensor = BLEMeshSensor(
        "loc", anchors={"l1": {"position": [0.0, 0.0, 2.0], "yaw_deg": 90.0}}
    )
    sensor.clock = lambda: 1.0

    class Src:
        t = 1.0

        def reports(self):
            return [
                TagReport("l1", "ble:TAG1", -60.0, 1.0, azimuth_rad=0.0, elevation_rad=0.0)
            ]

    sensor.source = Src()
    bearings = [o for o in sensor.poll() if isinstance(o, BearingObservation)]
    assert len(bearings) == 1
    # azimuth 0 in an anchor yawed 90 deg points along +y in the world
    assert bearings[0].direction[1] == pytest.approx(1.0, abs=1e-6)
    assert bearings[0].direction[0] == pytest.approx(0.0, abs=1e-6)


def test_calibrator_ignores_unidentifiable_geometry():
    """All reference samples at one distance cannot separate tx from exponent."""
    cal = PathLossCalibrator(PathLossModel(-59.0, 2.7))
    for _ in range(20):
        cal.observe("a1", -70.0, 3.0)
    assert not cal.fitted
    assert cal.model_for("a1").exponent == 2.7


def test_mesh_fix_flows_through_the_tracker_to_an_item():
    from hometwin.config import AppConfig
    from hometwin.items import Item, ItemRegistry
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    items = ItemRegistry()
    items.add(Item(item_id="keys", name="House keys", tag_ids=["ble:TAG1"]))
    sensor = _mesh()
    sensor.source = FakeMesh(CEILING, PathLossModel(-59.0, 2.7), {"ble:TAG1": TRUE}, noise_db=1.5)
    tracker = Tracker(
        AppConfig(world=World([Zone("room", (0, 0, 0), (5, 4, 3))]), items=items, sensors=[sensor])
    )
    for _ in range(10):
        tracker.step()
    entry = tracker.find("keys")
    assert entry["status"] == "tracked"
    assert math.dist([float(v) for v in entry["position"]], TRUE) < 1.5
