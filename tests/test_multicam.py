"""Multi-camera ray fusion: two viewpoints fix an item in true 3D."""

import math
import random

import pytest

from apartment_tracker.fusion import FusionEngine
from apartment_tracker.fusion.engine import ray_at_height, triangulate_rays
from apartment_tracker.items import Item, ItemRegistry
from apartment_tracker.observations import BearingObservation
from apartment_tracker.sensors.camera import CameraGeometry, CameraSensor, StaticFrameSource
from apartment_tracker.observations import Detection
from apartment_tracker.world import World, Zone


def ray_to(origin, target, jitter=None, rng=None):
    d = [t - o for t, o in zip(target, origin)]
    n = math.sqrt(sum(x * x for x in d))
    d = [x / n for x in d]
    if jitter and rng:
        d = [x + rng.gauss(0, jitter) for x in d]
        n = math.sqrt(sum(x * x for x in d))
        d = [x / n for x in d]
    return tuple(d)


def bearing(sensor_id, origin, target, ts, item="aruco:7", jitter=None, rng=None, label=None):
    return BearingObservation(
        sensor_id=sensor_id, timestamp=ts, item_id=item, label=label,
        origin=origin, direction=ray_to(origin, target, jitter, rng), sigma_rad=0.02,
    )


def make_engine():
    items = ItemRegistry()
    items.add(Item("keys", "Keys", labels=["keys"], tag_ids=["aruco:7"]))
    world = World([Zone("room", (0, 0, 0), (6, 5, 2.6))])
    return FusionEngine(items, world)


# --- triangulation -------------------------------------------------------------

def test_triangulate_two_rays_exact():
    target = (3.0, 2.0, 1.1)
    rays = [
        bearing("cam-a", (0.0, 0.0, 2.4), target, 1.0),
        bearing("cam-b", (6.0, 0.0, 2.4), target, 1.0),
    ]
    p = triangulate_rays(rays)
    assert p == pytest.approx(target, abs=1e-9)


def test_triangulate_parallel_rays_rejected():
    rays = [
        bearing("cam-a", (0.0, 0.0, 2.0), (5.0, 0.0, 2.0), 1.0),
        bearing("cam-b", (0.0, 1.0, 2.0), (5.0, 1.0, 2.0), 1.0),
    ]
    assert triangulate_rays(rays) is None


def test_ray_at_height_fallback():
    obs = bearing("cam-a", (0.0, 0.0, 2.4), (3.0, 2.0, 0.8), 1.0)
    p = ray_at_height(obs, 0.8)
    assert p == pytest.approx((3.0, 2.0, 0.8), abs=1e-9)


# --- engine fusion -------------------------------------------------------------

def test_two_cameras_fix_static_item():
    eng = make_engine()
    target = (3.0, 2.0, 1.1)
    rng = random.Random(5)
    cams = {"cam-a": (0.0, 0.0, 2.4), "cam-b": (6.0, 0.0, 2.4)}
    t = 0.0
    for _ in range(20):
        t += 0.5
        for cid, origin in cams.items():
            assert eng.ingest(bearing(cid, origin, target, t, jitter=0.01, rng=rng)) == "keys"
    track = eng.tracks["keys"]
    assert math.dist(track.position, target) < 0.25
    # depth is resolved by geometry, not the height prior: true z recovered
    assert abs(track.position[2] - 1.1) < 0.2


def test_single_camera_falls_back_to_height_prior():
    eng = make_engine()
    target = (3.0, 2.0, 0.8)  # at the prior height, where the fallback is exact
    origin = (0.0, 0.0, 2.4)
    for i in range(6):
        eng.ingest(bearing("cam-a", origin, target, 1.0 + i))
    track = eng.tracks.get("keys")
    assert track is not None
    assert math.dist(track.position, target) < 0.3


def test_two_cameras_track_moving_item():
    eng = make_engine()
    rng = random.Random(9)
    cams = {"cam-a": (0.0, 0.0, 2.4), "cam-b": (6.0, 4.8, 2.4)}
    t = 0.0
    err = None
    for step in range(60):
        t += 0.25
        # item carried across the room at ~0.5 m/s
        target = (1.0 + 0.125 * step * 0.5 * 2, 2.0 + 0.3 * math.sin(step / 8.0), 1.0)
        for cid, origin in cams.items():
            eng.ingest(bearing(cid, origin, target, t, jitter=0.01, rng=rng))
        err = math.dist(eng.tracks["keys"].position, target)
    assert err < 0.35  # still locked on at the end of the run


def test_anonymous_bearing_gating():
    eng = make_engine()
    target = (3.0, 2.0, 1.0)
    cams = {"cam-a": (0.0, 0.0, 2.4), "cam-b": (6.0, 0.0, 2.4)}
    t = 0.0
    for _ in range(10):
        t += 0.5
        for cid, origin in cams.items():
            eng.ingest(bearing(cid, origin, target, t))
    # an anonymous "keys" sighting in a very different direction is rejected
    impostor = bearing("cam-a", (0.0, 0.0, 2.4), (0.5, 4.5, 0.2), t + 1, item=None, label="keys")
    assert eng.ingest(impostor) is None
    # a consistent anonymous sighting refines the track
    good = bearing("cam-b", (6.0, 0.0, 2.4), target, t + 1, item=None, label="keys")
    assert eng.ingest(good) == "keys"


# --- camera sensor ray mode -----------------------------------------------------

class CenterDetector:
    def detect(self, frame):
        return [Detection(label="keys", confidence=0.9, bbox=(0.5, 0.5, 0, 0), tag_id="aruco:7")]


def test_camera_ray_mode_emits_bearing():
    geo = CameraGeometry(position=(1.0, 1.0, 2.0), yaw_deg=30, pitch_deg=45)
    cam = CameraSensor(
        "cam", geometry=geo, frame_source=StaticFrameSource([object()]),
        detector=CenterDetector(), mode="ray",
    )
    obs = cam.poll()
    assert len(obs) == 1
    assert isinstance(obs[0], BearingObservation)
    assert obs[0].origin == (1.0, 1.0, 2.0)
    assert obs[0].direction == pytest.approx(geo.ray(0.5, 0.5))


def test_camera_invalid_mode_rejected():
    geo = CameraGeometry(position=(0, 0, 2.0), yaw_deg=0, pitch_deg=45)
    with pytest.raises(ValueError):
        CameraSensor(
            "cam", geometry=geo, frame_source=StaticFrameSource([]),
            detector=CenterDetector(), mode="hologram",
        )


def test_two_ray_cameras_end_to_end():
    """Full path: pixels -> rays -> triangulated 3D, item NOT on any plane."""
    target = (3.0, 2.5, 1.3)  # mid-air: a surface-plane camera would get this wrong

    class TruthDetector:
        def __init__(self, geo):
            self.geo = geo

        def detect(self, frame):
            pix = self.geo.world_to_pixel(target)
            if pix is None:
                return []
            return [Detection(label="keys", confidence=1.0,
                              bbox=(pix[0], pix[1], 0, 0), tag_id="aruco:7")]

    eng = make_engine()
    cams = []
    for sid, pos, yaw in (("cam-a", (0.0, 0.0, 2.4), 40.0), ("cam-b", (6.0, 0.0, 2.4), 140.0)):
        geo = CameraGeometry(position=pos, yaw_deg=yaw, pitch_deg=20, hfov_deg=80)
        cams.append(CameraSensor(
            sid, geometry=geo, frame_source=StaticFrameSource([object()] * 10),
            detector=TruthDetector(geo), mode="ray",
        ))
    for _ in range(10):
        for cam in cams:
            for obs in cam.poll():
                eng.ingest(obs)
    track = eng.tracks["keys"]
    assert math.dist(track.position, target) < 0.1
