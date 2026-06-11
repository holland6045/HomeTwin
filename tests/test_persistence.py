import math

from hometwin.config import AppConfig
from hometwin.fusion import FusionEngine
from hometwin.items import Item, ItemRegistry
from hometwin.observations import PositionObservation
from hometwin.sensors.mock import ScriptedSensor
from hometwin.store import StateStore
from hometwin.tracker import Tracker
from hometwin.world import World, Zone


def make_parts():
    items = ItemRegistry()
    items.add(Item("keys", "Keys", tag_ids=["aruco:7"]))
    world = World([Zone("kitchen", (5, 0, 0), (8, 4, 2.6)), Zone("hall", (0, 0, 0), (5, 4, 2.6))])
    return items, world


def obs(pos, ts, sigma=0.1):
    return PositionObservation(
        sensor_id="cam", timestamp=ts, item_id="aruco:7", position=pos, sigma_m=sigma
    )


def test_engine_dump_restore_roundtrip():
    items, world = make_parts()
    eng = FusionEngine(items, world)
    for i in range(5):
        eng.ingest(obs((6.0, 1.0, 0.9), 100.0 + i))
    dumped = eng.dump_state()

    eng2 = FusionEngine(items, world)
    assert eng2.restore_state(dumped) == 1
    t1, t2 = eng.tracks["keys"], eng2.tracks["keys"]
    assert math.dist(t1.position, t2.position) < 1e-9
    assert t2.last_update == t1.last_update
    assert t2.observation_count == t1.observation_count


def test_restore_skips_unknown_items():
    items, world = make_parts()
    eng = FusionEngine(items, world)
    eng.ingest(obs((6.0, 1.0, 0.9), 100.0))
    dumped = eng.dump_state()
    dumped.append({**dumped[0], "item_id": "ghost"})
    eng2 = FusionEngine(items, world)
    assert eng2.restore_state(dumped) == 1
    assert "ghost" not in eng2.tracks


def test_store_atomic_and_corrupt_tolerant(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.load() == []
    store.save([{"item_id": "keys"}])
    assert store.load()[0]["item_id"] == "keys"
    (tmp_path / "state.json").write_text("{ not json")
    assert store.load() == []


def test_tracker_state_survives_restart(tmp_path):
    items, world = make_parts()
    state_path = str(tmp_path / "state.json")
    sensor = ScriptedSensor("cam", [[obs((6.0, 1.0, 0.9), 100.0 + i)] for i in range(5)])
    cfg = AppConfig(world=world, items=items, sensors=[sensor], state_path=state_path)
    t1 = Tracker(cfg)
    for _ in range(5):
        t1.step()
    t1.save_state()

    items2, world2 = make_parts()
    cfg2 = AppConfig(world=world2, items=items2, sensors=[], state_path=state_path)
    t2 = Tracker(cfg2)
    entry = t2.find("keys")
    assert entry["status"] in ("tracked", "stale")
    assert entry["zone"] == "kitchen"
    assert math.dist(entry["position"], (6.0, 1.0, 0.9)) < 0.2


def test_zone_change_events_recorded():
    items, world = make_parts()
    batches = [[obs((6.0, 1.0, 0.9), 100.0 + i, sigma=0.05)] for i in range(5)]
    batches += [[obs((2.0, 1.0, 0.9), 110.0 + i, sigma=0.05)] for i in range(8)]
    cfg = AppConfig(world=world, items=items, sensors=[ScriptedSensor("cam", batches)])
    tr = Tracker(cfg)
    for _ in range(len(batches)):
        tr.step()
    moves = [e for e in tr.events if e["to_zone"] == "hall"]
    assert moves and moves[0]["item_id"] == "keys"
    assert moves[0]["from_zone"] == "kitchen"


def test_restored_track_does_not_emit_spurious_event(tmp_path):
    items, world = make_parts()
    state_path = str(tmp_path / "state.json")
    sensor = ScriptedSensor("cam", [[obs((6.0, 1.0, 0.9), 100.0)]])
    t1 = Tracker(AppConfig(world=world, items=items, sensors=[sensor], state_path=state_path))
    t1.step()
    t1.save_state()

    items2, world2 = make_parts()
    sensor2 = ScriptedSensor("cam", [[obs((6.0, 1.0, 0.9), 200.0)]])
    t2 = Tracker(
        AppConfig(world=world2, items=items2, sensors=[sensor2], state_path=state_path)
    )
    t2.step()
    assert list(t2.events) == []
