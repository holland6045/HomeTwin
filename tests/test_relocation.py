"""People tracking through the fusion engine, and person-mediated item
relocation inference (the drawer-stow logic generalized to a carrier)."""

import pytest

from hometwin.config import AppConfig
from hometwin.items import Item, ItemRegistry
from hometwin.observations import BearingObservation, PositionObservation
from hometwin.sensors.mock import ScriptedSensor
from hometwin.tracker import Tracker
from hometwin.world import World, Zone


def make_world():
    return World([
        Zone("counter", (0, 0, 0), (2, 2, 1.2)),
        Zone("sofa", (4, 4, 0), (6, 6, 1.2)),
    ])


def person(ts, pos, sensor="cam"):
    return PositionObservation(sensor_id=sensor, timestamp=ts, item_id=None,
                               label="person", confidence=0.9, position=pos, sigma_m=0.2)


def keys(ts, pos):
    return PositionObservation(sensor_id="cam", timestamp=ts, item_id="keys",
                               label="keys", confidence=0.9, position=pos, sigma_m=0.05)


def tracker_with(sensor, items=None):
    reg = ItemRegistry()
    for it in (items or []):
        reg.add(it)
    cfg = AppConfig(world=make_world(), items=reg, sensors=[sensor],
                    presence_stale_after_s=5.0)
    return Tracker(cfg)


# --- people tracking ---------------------------------------------------------------

def test_person_gets_a_smoothed_track_not_an_item():
    s = ScriptedSensor("cam")
    tr = tracker_with(s)
    for i in range(4):
        s.push([person(100.0 + i, (1.0 + 0.2 * i, 1.0, 0.0))])
        tr.step()
    assert tr.engine.tracks == {}          # never an item track
    assert len(tr.engine.people) == 1
    p = tr.people[0]
    assert p["id"] == "person-1"
    assert p["zone"] == "counter"
    assert p["velocity"][0] > 0            # moving +x, velocity estimated
    assert tr.presence["zone"] == "counter"


def test_two_people_kept_separate_then_pruned():
    s = ScriptedSensor("cam")
    tr = tracker_with(s)
    for i in range(3):
        s.push([person(100.0 + i, (1.0, 1.0, 0.0)),
                person(100.0 + i, (5.0, 5.0, 0.0))])
        tr.step()
    assert len(tr.engine.people) == 2
    # silence past the stale window: both age out
    s.push([person(130.0, (5.0, 5.0, 0.0))])
    tr.step()
    assert [p["id"] for p in tr.people] == ["person-2"] or len(tr.people) == 1


def test_bearing_mode_person_tracked():
    s = ScriptedSensor("cam")
    tr = tracker_with(s)
    obs = BearingObservation(sensor_id="cam", timestamp=100.0, item_id=None,
                             label="person", confidence=0.9, origin=(0, 0, 2.0),
                             direction=(0.5, 0.5, -0.5), sigma_rad=0.05)
    s.push([obs])
    tr.step()
    assert len(tr.engine.people) == 1


# --- relocation inference ----------------------------------------------------------

def test_carried_item_follows_person_to_drop_location():
    s = ScriptedSensor("cam")
    tr = tracker_with(s, [Item("keys", "House keys", labels=["keys"])])

    # keys rest on the counter; a person walks up to them
    for i in range(3):
        s.push([keys(100.0 + i, (1.0, 1.0, 0.9)), person(100.0 + i, (1.2, 1.0, 0.0))])
        tr.step()
    assert tr.engine.tracks["keys"].position[0] == pytest.approx(1.0, abs=0.3)

    # keys vanish (pocketed); the person carries them toward the sofa
    path = [(2.0, 2.0), (3.0, 3.0), (4.5, 4.5), (5.0, 5.0)]
    for j, (x, y) in enumerate(path):
        s.push([person(104.0 + j, (x, y, 0.0))])
        tr.step()
    # person now stationary at the sofa for a couple ticks -> settles
    for j in range(3):
        s.push([person(108.0 + j, (5.0, 5.0, 0.0))])
        tr.step()

    entry = tr.find("keys")
    assert entry["maybe_carried_by"] == "person-1"
    assert entry["likely_zone"] == "sofa"
    assert entry["likely_position"][0] == pytest.approx(5.0, abs=0.5)


def test_item_reappearing_clears_the_carry():
    s = ScriptedSensor("cam")
    tr = tracker_with(s, [Item("keys", "House keys", labels=["keys"])])
    for i in range(3):
        s.push([keys(100.0 + i, (1.0, 1.0, 0.9)), person(100.0 + i, (1.2, 1.0, 0.0))])
        tr.step()
    for j in range(4):  # keys gone, person wanders off
        s.push([person(104.0 + j, (3.0 + j, 3.0 + j, 0.0))])
        tr.step()
    assert "keys" in tr._carries
    # keys turn up again on the counter: the live track supersedes the guess
    s.push([keys(120.0, (1.0, 1.0, 0.9))])
    tr.step()
    assert "keys" not in tr._carries
    assert tr.find("keys").get("maybe_carried_by") is None


def test_item_quietly_lost_without_contact_is_not_carried():
    s = ScriptedSensor("cam")
    tr = tracker_with(s, [Item("keys", "House keys", labels=["keys"])])
    # keys seen, but no person ever near them
    for i in range(3):
        s.push([keys(100.0 + i, (1.0, 1.0, 0.9))])
        tr.step()
    for j in range(5):
        s.push([])  # nothing at all
        tr.step()
    assert "keys" not in tr._carries
    assert tr.find("keys").get("maybe_carried_by") is None


def test_person_localizes_from_feet_not_centroid():
    """A standing person projects from the bbox bottom (feet) onto the floor;
    an item with the same box projects from its centroid onto the surface."""
    from hometwin.observations import Detection
    from hometwin.sensors.camera import CameraGeometry, CameraSensor

    geo = CameraGeometry(position=(2.0, 0.0, 2.2), yaw_deg=90, pitch_deg=45)

    class Frames:
        def get_frame(self):
            return None

    cam = CameraSensor("cam", geometry=geo, frame_source=Frames(),
                       detector=object(), surface_z=0.0)
    cam.attach_presence_labels(["person"])
    box = (0.4, 0.2, 0.2, 0.6)  # x,y,w,h ; center v=0.5, bottom v=0.8
    person_det = Detection(label="person", confidence=0.9, bbox=box)
    item_det = Detection(label="keys", confidence=0.9, bbox=box)

    p_person = cam.to_observation(person_det, 100.0).position
    p_item = cam.to_observation(item_det, 100.0).position
    # feet are lower in the image than the centroid -> project nearer the
    # camera in y than the centroid would
    assert p_person[1] != pytest.approx(p_item[1], abs=0.1)
    # occupant carries a coarser sigma than an item fix
    assert cam.to_observation(person_det, 100.0).sigma_m > \
        cam.to_observation(item_det, 100.0).sigma_m


def test_relocation_emits_events():
    s = ScriptedSensor("cam")
    tr = tracker_with(s, [Item("keys", "House keys", labels=["keys"])])
    for i in range(3):
        s.push([keys(100.0 + i, (1.0, 1.0, 0.9)), person(100.0 + i, (1.2, 1.0, 0.0))])
        tr.step()
    for j, (x, y) in enumerate([(2.0, 2.0), (3.0, 3.0), (4.5, 4.5), (5.0, 5.0)]):
        s.push([person(104.0 + j, (x, y, 0.0))]); tr.step()
    for j in range(3):
        s.push([person(108.0 + j, (5.0, 5.0, 0.0))]); tr.step()
    kinds = [e.get("event") for e in tr.events]
    assert "picked_up" in kinds
    placed = [e for e in tr.events if e.get("event") == "placed"]
    assert placed and placed[-1]["placed_in"] == "sofa"


def test_carry_hypothesis_survives_restart(tmp_path):
    state = str(tmp_path / "state.json")

    def build():
        reg = ItemRegistry(); reg.add(Item("keys", "House keys", labels=["keys"]))
        cfg = AppConfig(world=make_world(), items=reg, sensors=[ScriptedSensor("cam")],
                        presence_stale_after_s=5.0, state_path=state)
        return Tracker(cfg), cfg.sensors[0]

    tr, s = build()
    for i in range(3):
        s.push([keys(100.0 + i, (1.0, 1.0, 0.9)), person(100.0 + i, (1.2, 1.0, 0.0))])
        tr.step()
    for j, (x, y) in enumerate([(2.0, 2.0), (3.5, 3.5), (5.0, 5.0)]):
        s.push([person(104.0 + j, (x, y, 0.0))]); tr.step()
    for j in range(3):
        s.push([person(107.0 + j, (5.0, 5.0, 0.0))]); tr.step()
    assert tr.find("keys")["likely_zone"] == "sofa"
    tr.save_state()

    # fresh process: item track restores to its last real fix (the counter),
    # but the carry hypothesis says it was last carried to the sofa
    tr2, _ = build()
    entry = tr2.find("keys")
    assert entry["maybe_carried_by"] == "person-1"
    assert entry["likely_zone"] == "sofa"
