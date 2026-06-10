import math

from apartment_tracker.fusion import FusionEngine
from apartment_tracker.items import Item, ItemRegistry
from apartment_tracker.observations import (
    AreaObservation,
    PositionObservation,
    RangeObservation,
)
from apartment_tracker.world import World, Zone


def make_engine():
    items = ItemRegistry()
    items.add(Item("keys", "Keys", labels=["keys"], tag_ids=["aruco:7"]))
    items.add(Item("wallet", "Wallet", tag_ids=["ble:AA:BB:CC:DD:EE:FF"]))
    world = World([Zone("kitchen", (5, 0, 0), (8, 4, 2.6))])
    return FusionEngine(items, world)


def test_tagged_position_observation_tracks_item():
    eng = make_engine()
    obs = PositionObservation(
        sensor_id="cam", timestamp=100.0, item_id="aruco:7", position=(6.0, 1.0, 0.9), sigma_m=0.2
    )
    assert eng.ingest(obs) == "keys"
    snap = {e["item_id"]: e for e in eng.snapshot(now=101.0)}
    assert snap["keys"]["status"] == "tracked"
    assert snap["keys"]["zone"] == "kitchen"
    assert snap["wallet"]["status"] == "never_seen"


def test_unknown_tag_ignored():
    eng = make_engine()
    obs = PositionObservation(
        sensor_id="cam", timestamp=100.0, item_id="aruco:99", position=(1, 1, 1)
    )
    assert eng.ingest(obs) is None


def test_anonymous_label_gating():
    eng = make_engine()
    t = 100.0
    for i in range(10):
        eng.ingest(
            PositionObservation(
                sensor_id="cam", timestamp=t + i, item_id="aruco:7",
                position=(6.0, 1.0, 0.9), sigma_m=0.1,
            )
        )
    # an anonymous "keys" detection across the apartment must not hijack the track
    far = PositionObservation(
        sensor_id="cam2", timestamp=t + 11, label="keys", position=(0.5, 0.5, 0.0), sigma_m=0.1
    )
    assert eng.ingest(far) is None
    # but a nearby anonymous detection refines it
    near = PositionObservation(
        sensor_id="cam2", timestamp=t + 11, label="keys", position=(6.1, 1.05, 0.9), sigma_m=0.1
    )
    assert eng.ingest(near) == "keys"
    pos = eng.tracks["keys"].position
    assert math.dist(pos, (6.0, 1.0, 0.9)) < 0.3


def test_range_observations_trilaterate():
    eng = make_engine()
    true_pos = (6.0, 2.0, 1.0)
    anchors = [(0.0, 0.0, 2.2), (8.0, 0.0, 2.2), (0.0, 4.0, 2.2), (8.0, 4.0, 2.2)]
    t = 0.0
    for _ in range(50):
        t += 0.5
        for a in anchors:
            eng.ingest(
                RangeObservation(
                    sensor_id="ble", timestamp=t, item_id="ble:AA:BB:CC:DD:EE:FF",
                    anchor=a, range_m=math.dist(true_pos, a), sigma_m=0.5,
                )
            )
    assert math.dist(eng.tracks["wallet"].position, true_pos) < 0.5


def test_multimodal_fusion_tightens_estimate():
    eng = make_engine()
    true_pos = (6.0, 1.0, 0.9)
    for anchor in ((0.0, 0.0, 2.2), (8.0, 0.0, 2.2), (0.0, 4.0, 2.2)):
        eng.ingest(
            RangeObservation(
                sensor_id="ble", timestamp=1.0, item_id="aruco:7",
                anchor=anchor, range_m=math.dist(true_pos, anchor), sigma_m=1.0,
            )
        )
    sigma_ble_only = eng.tracks["keys"].sigma_m
    eng.ingest(
        PositionObservation(
            sensor_id="cam", timestamp=1.5, item_id="aruco:7",
            position=(6.0, 1.0, 0.9), sigma_m=0.2,
        )
    )
    assert eng.tracks["keys"].sigma_m < sigma_ble_only
    assert set(eng.tracks["keys"].contributors) == {"ble", "cam"}


def test_area_observation_updates_coarsely():
    eng = make_engine()
    eng.ingest(
        AreaObservation(
            sensor_id="rti", timestamp=1.0, item_id="aruco:7", centroid=(6, 2, 1), sigma_m=1.5
        )
    )
    track = eng.tracks["keys"]
    assert track.sigma_m > 0.5  # coarse, but present


def test_staleness_reported():
    eng = make_engine()
    eng.ingest(
        PositionObservation(
            sensor_id="cam", timestamp=100.0, item_id="aruco:7", position=(6, 1, 0.9)
        )
    )
    snap = {e["item_id"]: e for e in eng.snapshot(now=100.0 + 301.0)}
    assert snap["keys"]["status"] == "stale"
