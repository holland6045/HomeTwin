"""Passive world model: accumulation, decay-as-merge, persistence, serving."""

import json
import urllib.request

from hometwin.worldmodel import MIN_WEIGHT, WorldModel


def test_voxel_binning_merges_nearby_points():
    wm = WorldModel(voxel_m=0.2)
    for _ in range(10):
        wm.add_point((1.01, 2.02, 0.5), ts=100.0)
    wm.add_point((5.0, 5.0, 1.0), ts=100.0)
    assert len(wm.voxels) == 2
    cloud = wm.point_cloud()
    assert cloud[0][3] > cloud[1][3]  # heaviest first
    assert abs(cloud[0][0] - 1.1) < 0.11  # voxel center near the deposits


def test_decay_is_the_merge_policy():
    wm = WorldModel(voxel_m=0.2, half_life_s=100.0)
    wm.add_point((0, 0, 0), ts=0.0, weight=1.0)
    wm.decay_and_compact(now=100.0)  # one half-life
    assert abs(wm.voxels[wm._key((0, 0, 0))][0] - 0.5) < 1e-9
    # fresh evidence on a decayed voxel: old fades, new dominates
    wm.add_point((0, 0, 0), ts=200.0, weight=1.0)
    assert abs(wm.voxels[wm._key((0, 0, 0))][0] - 1.25) < 1e-9


def test_floaters_dropped_and_cap_enforced():
    wm = WorldModel(voxel_m=0.1, half_life_s=10.0, max_voxels=50)
    wm.add_point((9, 9, 9), ts=0.0, weight=MIN_WEIGHT * 1.5)  # a floater
    for i in range(80):
        wm.add_point((i * 0.5, 0, 0), ts=1000.0, weight=5.0)
    removed = wm.decay_and_compact(now=1000.0)
    assert removed > 0
    assert len(wm.voxels) <= 50
    assert wm._key((9, 9, 9)) not in wm.voxels  # decayed below the floor


def test_persistence_roundtrip(tmp_path):
    wm = WorldModel(voxel_m=0.15)
    wm.add_point((1, 2, 0.5), ts=50.0, weight=2.0)
    wm.add_point((3, 1, 1.5), ts=60.0)
    path = tmp_path / "wm.gz"
    wm.save(path)
    loaded = WorldModel.load(path)
    assert loaded.voxels == wm.voxels
    assert loaded.observations == wm.observations
    assert WorldModel.load(tmp_path / "missing.gz").voxels == {}  # graceful


def test_simulation_builds_a_cloud_and_serves_it():
    from hometwin.api import ApiServer
    from hometwin.simulate import build_simulation

    tracker, state = build_simulation(seed=1)
    for _ in range(80):
        state.tick()
        tracker.step()
    stats = tracker.worldmodel.stats()
    assert stats["voxels"] > 10  # moving phone + presence sketched space
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        base = f"http://127.0.0.1:{api.port}"
        with urllib.request.urlopen(f"{base}/pointcloud", timeout=5) as r:
            d = json.loads(r.read())
        assert d["voxels"] == stats["voxels"]
        assert len(d["points"]) == stats["voxels"]
        assert all(len(p) == 4 for p in d["points"])
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            assert json.loads(r.read())["worldmodel"]["voxels"] > 10
    finally:
        api.stop()


def test_worldmodel_persists_with_tracker_state(tmp_path):
    from hometwin.config import AppConfig
    from hometwin.items import Item, ItemRegistry
    from hometwin.observations import PositionObservation
    from hometwin.sensors.mock import ScriptedSensor
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    items = ItemRegistry()
    items.add(Item("keys", "Keys", tag_ids=["aruco:7"]))
    world = World([Zone("room", (0, 0, 0), (5, 5, 2.6))])
    sensor = ScriptedSensor("cam", [[PositionObservation(
        sensor_id="cam", timestamp=100.0 + i, item_id="aruco:7",
        position=(1.0 + i * 0.3, 2.0, 0.8), sigma_m=0.05)] for i in range(5)])
    state_path = str(tmp_path / "state.json")
    t1 = Tracker(AppConfig(world=world, items=items, sensors=[sensor],
                           state_path=state_path))
    for _ in range(5):
        t1.step()
    t1.save_state()

    t2 = Tracker(AppConfig(world=world, items=items, sensors=[],
                           state_path=state_path))
    assert t2.worldmodel.stats()["voxels"] == t1.worldmodel.stats()["voxels"] > 0


def test_pointcloud_svg_renders():
    from hometwin.render import pointcloud_svg
    from hometwin.simulate import build_simulation

    tracker, state = build_simulation(seed=1)
    for _ in range(60):
        state.tick()
        tracker.step()
    svg = pointcloud_svg(tracker.worldmodel.point_cloud())
    assert svg.startswith("<svg") and "WORLD MODEL" in svg
    assert svg.count("<circle") == tracker.worldmodel.stats()["voxels"]
    assert pointcloud_svg([]).count("<circle") == 0  # empty model: message
