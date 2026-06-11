"""Soft references: non-anchor tags improving multi-camera registration.

A tag with a tight, stationary, multi-sensor-confirmed fused position
teaches drifted cameras (cross-camera registration); a confidently-closed
movable's home does the same. Gates ensure no feedback loops.
"""

import math

import pytest

from hometwin.anchors import AnchorCalibrator
from hometwin.config import AppConfig
from hometwin.items import Item, ItemRegistry
from hometwin.movables import CLOSED_REF_STREAK, Movable, MovableRegistry
from hometwin.observations import Detection
from hometwin.sensors.camera import CameraGeometry, CameraSensor
from hometwin.tracker import Tracker
from hometwin.world import World, Zone

REF_POS = (4.0, 2.0, 0.9)


def make_geos(yaw_err=0.0, pitch_err=0.0):
    true_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=0, pitch_deg=18)
    cfg_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=yaw_err, pitch_deg=18 + pitch_err)
    return true_geo, cfg_geo


# --- calibrator level -----------------------------------------------------------

def test_soft_reference_corrects_drift():
    true_geo, cfg_geo = make_geos(yaw_err=2.0)
    cal = AnchorCalibrator(cfg_geo, {}, auto_correct=True)
    for i in range(150):
        pix = true_geo.world_to_pixel(REF_POS)
        cal.observe_soft("aruco:7", pix[0], pix[1], float(i), REF_POS, sigma_m=0.05)
    assert abs(math.degrees(cfg_geo.yaw)) < 0.2
    s = cal.status()
    assert s["soft_observations"] == 150
    assert s["soft_residual_deg"] < 0.2


def test_loose_reference_teaches_nothing():
    true_geo, cfg_geo = make_geos(yaw_err=2.0)
    cal = AnchorCalibrator(cfg_geo, {}, auto_correct=True)
    pix = true_geo.world_to_pixel(REF_POS)
    for i in range(50):  # sigma 0.5 m at 4 m ~ 7 deg angular: above the gate
        cal.observe_soft("aruco:7", pix[0], pix[1], float(i), REF_POS, sigma_m=0.5)
    assert math.degrees(cfg_geo.yaw) == pytest.approx(2.0, abs=1e-9)
    assert cal.status()["soft_observations"] == 0


def test_soft_weighted_below_hard_anchor():
    """When a hard anchor and a soft ref disagree, the anchor wins."""
    true_geo, cfg_geo = make_geos(yaw_err=0.0)
    anchor_pos = (4.0, 1.0, 1.2)
    cal = AnchorCalibrator(cfg_geo, {"aruco:100": anchor_pos}, auto_correct=True)
    bogus_ref = (4.0, 2.3, 0.9)  # fused estimate 0.3 m off reality
    for i in range(80):
        apix = true_geo.world_to_pixel(anchor_pos)
        cal.observe("aruco:100", apix[0], apix[1], float(i))
        rpix = true_geo.world_to_pixel(REF_POS)  # true tag position
        cal.observe_soft("aruco:7", rpix[0], rpix[1], float(i), bogus_ref, sigma_m=0.15)
    # the surveyed anchor dominates: pose stays within a fraction of the
    # ~4 deg pull the bogus reference is asking for
    assert abs(math.degrees(cfg_geo.yaw)) < 0.8
    assert cal.status()["healthy"] is True  # soft residual never flags health


# --- tracker-level gating -------------------------------------------------------

def make_tracker(cameras, items=None):
    reg = ItemRegistry()
    for it in items or [Item("keys", "Keys", tag_ids=["aruco:7"])]:
        reg.add(it)
    world = World([Zone("room", (0, 0, 0), (8, 4, 2.6))])
    return Tracker(AppConfig(world=world, items=reg, sensors=cameras))


class TagDetector:
    """Sees the tag where the PHYSICAL camera would (true geometry)."""

    def __init__(self, true_geo, pos=REF_POS):
        self.true_geo, self.pos = true_geo, pos

    def detect(self, frame):
        pix = self.true_geo.world_to_pixel(self.pos)
        if pix is None:
            return []
        return [Detection(label="keys", confidence=1.0, bbox=(pix[0], pix[1], 0, 0),
                          tag_id="aruco:7")]


class EndlessFrames:
    def get_frame(self):
        return object()


class SimClock:
    def __init__(self):
        self.now = 1000.0

    def advance(self, dt=0.5):
        self.now += dt

    def __call__(self):
        return self.now


def test_two_cameras_cross_register():
    """Camera A (true pose) localizes the tag; bumped camera B self-corrects
    against the fused estimate — no surveyed anchor anywhere."""
    geo_a = CameraGeometry(position=(8.0, 2.0, 2.2), yaw_deg=180, pitch_deg=18)
    cam_a = CameraSensor("cam-a", geometry=geo_a, frame_source=EndlessFrames(),
                         detector=TagDetector(geo_a), surface_z=REF_POS[2],
                         base_sigma_m=0.04)
    true_b, cfg_b = make_geos(yaw_err=2.0)
    cam_b = CameraSensor("cam-b", geometry=cfg_b, frame_source=EndlessFrames(),
                         detector=TagDetector(true_b), surface_z=REF_POS[2],
                         base_sigma_m=0.04, anchor_correct=True)
    tracker = make_tracker([cam_a, cam_b])
    clock = SimClock()
    cam_a.clock = cam_b.clock = clock
    for _ in range(120):
        clock.advance()
        tracker.step()
    # consensus, not truth: cam-b's own biased observations also feed the
    # fused reference, so mutual registration halves the error rather than
    # zeroing it — absolute accuracy still comes from surveyed anchors
    assert abs(math.degrees(cfg_b.yaw)) < 1.0  # was 2.0
    assert cam_b.calibrator.status()["soft_observations"] > 0


def test_camera_never_calibrates_against_itself():
    """One camera alone: its own track must not come back as a reference."""
    true_geo, cfg_geo = make_geos(yaw_err=2.0)
    cam = CameraSensor("cam-solo", geometry=cfg_geo, frame_source=EndlessFrames(),
                       detector=TagDetector(true_geo), surface_z=REF_POS[2],
                       anchor_correct=True)
    tracker = make_tracker([cam])
    for _ in range(60):
        tracker.step()
    assert math.degrees(cfg_geo.yaw) == pytest.approx(2.0, abs=1e-9)
    assert cam._soft_refs == {}


def test_moving_item_not_a_reference():
    geo_a = CameraGeometry(position=(8.0, 2.0, 2.2), yaw_deg=180, pitch_deg=18)

    class MovingTag:
        def __init__(self, geo):
            self.geo, self.t = geo, 0.0

        def detect(self, frame):
            self.t += 0.5
            pos = (4.0 + 0.5 * self.t % 2.0, 2.0, 0.9)  # keeps moving
            pix = self.geo.world_to_pixel(pos)
            return [Detection(label="keys", confidence=1.0,
                              bbox=(pix[0], pix[1], 0, 0), tag_id="aruco:7")] if pix else []

    cam_a = CameraSensor("cam-a", geometry=geo_a, frame_source=EndlessFrames(),
                         detector=MovingTag(geo_a), surface_z=0.9)
    _, cfg_b = make_geos(yaw_err=2.0)
    cam_b = CameraSensor("cam-b", geometry=cfg_b, frame_source=EndlessFrames(),
                         detector=lambda: None, surface_z=0.9)
    cam_b.detector = type("Empty", (), {"detect": lambda self, f: []})()
    tracker = make_tracker([cam_a, cam_b])
    clock = SimClock()
    cam_a.clock = cam_b.clock = clock
    for _ in range(40):
        clock.advance()
        tracker.step()
    assert cam_b._soft_refs == {}  # a moving tag teaches nobody


# --- movables as conditional references ------------------------------------------

DRAWER = Movable(name="drawer", tag="aruco:40", motion="slide",
                 home=(4.0, 2.0, 0.6), axis=(0, 1, 0), travel=0.4)


class DrawerTagDetector:
    def __init__(self, true_geo, openness=0.0):
        self.true_geo, self.openness = true_geo, openness

    def detect(self, frame):
        pix = self.true_geo.world_to_pixel(DRAWER.position_at(self.openness))
        if pix is None:
            return []
        return [Detection(label="aruco", confidence=1.0, bbox=(pix[0], pix[1], 0, 0),
                          tag_id="aruco:40")]


def test_closed_drawer_home_corrects_pitch_drift():
    # pitch drift displaces the tag perpendicular to the slide axis, so the
    # drawer still reads closed -> home qualifies as a reference and heals it
    true_geo, cfg_geo = make_geos(pitch_err=1.5)
    cam = CameraSensor("cam", geometry=cfg_geo, frame_source=EndlessFrames(),
                       detector=DrawerTagDetector(true_geo), surface_z=0.6,
                       anchor_correct=True)
    cam.attach_movables(MovableRegistry([DRAWER]))
    for i in range(80):
        cam.poll()
    assert abs(math.degrees(cfg_geo.pitch) - 18.0) < 0.2
    assert cam.calibrator.status()["soft_observations"] > 0


def test_open_drawer_never_a_reference():
    true_geo, cfg_geo = make_geos()
    cam = CameraSensor("cam", geometry=cfg_geo, frame_source=EndlessFrames(),
                       detector=DrawerTagDetector(true_geo, openness=0.6), surface_z=0.6,
                       anchor_correct=True)
    reg = MovableRegistry([DRAWER])
    cam.attach_movables(reg)
    for _ in range(CLOSED_REF_STREAK * 3):
        cam.poll()
    assert reg.closed_reference("aruco:40") is None
    assert cam.calibrator is None or cam.calibrator.soft_observations == 0
