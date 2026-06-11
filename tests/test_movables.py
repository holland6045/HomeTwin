"""Tags on moving surfaces: doors/drawers as one-DOF state sensors."""

import math

import pytest

from hometwin.config import AppConfig, load_config
from hometwin.items import Item, ItemRegistry
from hometwin.movables import Movable, MovableRegistry
from hometwin.observations import BearingObservation, PositionObservation
from hometwin.sensors.camera import CameraGeometry
from hometwin.sensors.mock import ScriptedSensor
from hometwin.simulate import build_simulation
from hometwin.tracker import Tracker
from hometwin.world import Spot, World, Zone

CAM = CameraGeometry(position=(0.0, 0.0, 2.2), yaw_deg=45, pitch_deg=25, hfov_deg=80)

DRAWER = Movable(
    name="drawer-2", tag="aruco:40", motion="slide",
    home=(2.0, 2.0, 0.6), axis=(0, 1, 0), travel=0.4, spot="drawer-2",
)
DOOR = Movable(
    name="closet-door", tag="aruco:41", motion="hinge",
    home=(3.0, 1.0, 1.2), hinge=(2.6, 1.0), max_angle_deg=100.0,
)


def ray_to(p):
    d = [p[i] - CAM.position[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in d))
    return tuple(x / n for x in d)


def observe_at(reg, movable, fraction, ts):
    pos = movable.position_at(fraction)
    assert reg.observe_ray(movable.tag, CAM.position, ray_to(pos), ts)


def test_slide_openness_estimated():
    reg = MovableRegistry([DRAWER], alpha=1.0)
    for frac in (0.0, 0.5, 1.0):
        observe_at(reg, DRAWER, frac, 1.0)
        assert reg.states["drawer-2"].openness == pytest.approx(frac, abs=0.02)


def test_hinge_openness_estimated():
    reg = MovableRegistry([DOOR], alpha=1.0)
    for frac in (0.0, 0.3, 0.9):
        observe_at(reg, DOOR, frac, 1.0)
        assert reg.states["closet-door"].openness == pytest.approx(frac, abs=0.03)


def test_open_close_events_with_hysteresis():
    reg = MovableRegistry([DRAWER], alpha=1.0)
    observe_at(reg, DRAWER, 0.05, 1.0)   # closed
    observe_at(reg, DRAWER, 0.15, 2.0)   # above close, below open: no event
    observe_at(reg, DRAWER, 0.8, 3.0)    # -> opened
    observe_at(reg, DRAWER, 0.15, 4.0)   # hysteresis: still open
    observe_at(reg, DRAWER, 0.02, 5.0)   # -> closed
    events = reg.drain_events()
    assert [(e["event"], e["timestamp"]) for e in events] == [("opened", 3.0), ("closed", 5.0)]
    assert reg.drain_events() == []


def test_unknown_tag_not_consumed():
    reg = MovableRegistry([DRAWER])
    assert reg.observe_ray("aruco:99", CAM.position, (1, 0, 0), 1.0) is False


def test_movable_validation():
    with pytest.raises(ValueError, match="slide"):
        Movable(name="x", tag="t", motion="slide", home=(0, 0, 0))
    with pytest.raises(ValueError, match="hinge"):
        Movable(name="x", tag="t", motion="hinge", home=(0, 0, 0))
    with pytest.raises(ValueError, match="motion"):
        Movable(name="x", tag="t", motion="teleport", home=(0, 0, 0))


def test_config_movables_and_anchor_exclusion(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        """
world:
  zones: []
  anchors:
    - {tag: "aruco:40", position: [2.0, 2.0, 0.6]}   # mistake: it's on a drawer
  movables:
    - name: drawer-2
      tag: "aruco:40"
      motion: slide
      home: [2.0, 2.0, 0.6]
      axis: [0, 1, 0]
      travel: 0.4
      spot: drawer-2
    - name: closet-door
      tag: "aruco:41"
      motion: hinge
      home: [3.0, 1.0, 1.2]
      hinge: [2.6, 1.0]
      max_angle_deg: 100
items: []
sensors: []
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.movables is not None
    assert set(cfg.movables.by_tag) == {"aruco:40", "aruco:41"}
    assert "aruco:40" not in cfg.anchors  # moving surface never calibrates


def make_tracker_with_drawer():
    items = ItemRegistry()
    items.add(Item("keys", "Keys", tag_ids=["aruco:7"]))
    world = World(
        [Zone("office", (0, 0, 0), (4, 4, 2.6))],
        [Spot("drawer-2", (2.0, 1.9, 0.6), radius=0.25)],
    )
    reg = MovableRegistry([DRAWER], alpha=1.0)
    sensor = ScriptedSensor("feed")
    cfg = AppConfig(world=world, items=items, sensors=[sensor], movables=reg)
    return Tracker(cfg), reg, sensor


def test_bridge_bearings_routed_to_movables():
    tracker, reg, sensor = make_tracker_with_drawer()
    pos = DRAWER.position_at(0.9)
    sensor.push([BearingObservation(
        sensor_id="esp32-cam", timestamp=5.0, item_id="aruco:40",
        origin=CAM.position, direction=ray_to(pos),
    )])
    tracker.step()
    assert reg.states["drawer-2"].is_open
    assert any(e.get("event") == "opened" for e in tracker.events)
    assert "aruco:40" not in tracker.engine.tracks  # never an item track


def test_stowed_in_inference():
    """Keys seen at the open drawer, drawer closes, keys unseen -> likely_in."""
    tracker, reg, sensor = make_tracker_with_drawer()

    def keys_at(ts):
        return PositionObservation(
            sensor_id="cam", timestamp=ts, item_id="aruco:7",
            position=(2.0, 1.9, 0.62), sigma_m=0.05,
        )

    observe_at(reg, DRAWER, 0.9, 10.0)        # drawer opens
    sensor.push([keys_at(11.0)])
    tracker.step()
    sensor.push([keys_at(12.0)])              # keys last seen at the drawer
    tracker.step()
    observe_at(reg, DRAWER, 0.02, 15.0)       # drawer closes; keys never seen again
    tracker.step()

    snap = {e["item_id"]: e for e in tracker.snapshot()}
    assert snap["keys"]["maybe_in"] == "drawer-2"

    # keys reappear elsewhere: the hint must clear
    sensor.push([PositionObservation(
        sensor_id="cam", timestamp=20.0, item_id="aruco:7",
        position=(0.5, 0.5, 0.8), sigma_m=0.05,
    )])
    tracker.step()
    snap = {e["item_id"]: e for e in tracker.snapshot()}
    assert "maybe_in" not in snap["keys"]


def test_simulation_drawer_cycle():
    tracker, state = build_simulation(seed=4)
    opened = closed = False
    for _ in range(70):  # 35 s of sim time covers the 10-25 s drawer cycle
        state.tick()
        tracker.step()
        for e in tracker.events:
            opened |= e.get("event") == "opened"
            closed |= e.get("event") == "closed"
    assert opened and closed
    from hometwin.overlay import map_overlay

    d = map_overlay(tracker)
    assert d["movables"][0]["name"] == "utensil-drawer"
    assert d["movables"][0]["is_open"] is False
