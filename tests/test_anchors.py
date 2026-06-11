"""Blocky calibration anchors: drift reporting and bounded auto-correction."""

import math

import pytest

from apartment_tracker.anchors import AnchorCalibrator
from apartment_tracker.observations import Detection, PositionObservation
from apartment_tracker.sensors.camera import CameraGeometry, CameraSensor, StaticFrameSource
from apartment_tracker.simulate import ANCHOR_TAG, CAM_KITCHEN_YAW_TRUE, build_simulation

ANCHOR = {"aruco:100": (4.0, 2.0, 0.9)}


def observe_through(true_geo, cfg_calibrator, anchor_pos, times=1, ts=1.0):
    """Feed the calibrator pixels computed by the *physical* camera."""
    for i in range(times):
        pix = true_geo.world_to_pixel(anchor_pos)
        assert pix is not None
        assert cfg_calibrator.observe("aruco:100", pix[0], pix[1], ts + i)


def test_aligned_camera_zero_residual():
    geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=30, pitch_deg=20)
    cal = AnchorCalibrator(geo, ANCHOR, auto_correct=True)
    observe_through(geo, cal, ANCHOR["aruco:100"], times=5)
    s = cal.status()
    assert s["residual_deg"] == pytest.approx(0.0, abs=1e-6)
    assert s["correction_yaw_deg"] == pytest.approx(0.0, abs=1e-6)
    assert s["healthy"] is True
    assert s["anchors"] == ["aruco:100"]


def test_yaw_drift_corrected():
    true_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=30, pitch_deg=20)
    cfg_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=33, pitch_deg=20)  # bumped 3 deg
    cal = AnchorCalibrator(cfg_geo, ANCHOR, auto_correct=True)
    observe_through(true_geo, cal, ANCHOR["aruco:100"], times=60)
    assert math.degrees(cfg_geo.yaw) == pytest.approx(30.0, abs=0.05)
    s = cal.status()
    assert s["correction_yaw_deg"] == pytest.approx(-3.0, abs=0.05)
    assert s["healthy"] is True


def test_pitch_drift_corrected():
    true_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=30, pitch_deg=20)
    cfg_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=30, pitch_deg=18)
    cal = AnchorCalibrator(cfg_geo, ANCHOR, auto_correct=True)
    observe_through(true_geo, cal, ANCHOR["aruco:100"], times=60)
    assert math.degrees(cfg_geo.pitch) == pytest.approx(20.0, abs=0.05)
    assert cal.status()["correction_pitch_deg"] == pytest.approx(2.0, abs=0.05)


def test_report_only_mode_never_mutates_pose():
    true_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=30, pitch_deg=20)
    cfg_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=33, pitch_deg=20)
    cal = AnchorCalibrator(cfg_geo, ANCHOR, auto_correct=False)
    observe_through(true_geo, cal, ANCHOR["aruco:100"], times=20)
    assert math.degrees(cfg_geo.yaw) == pytest.approx(33.0, abs=1e-9)
    s = cal.status()
    assert s["residual_deg"] == pytest.approx(3.0, abs=0.3)
    assert s["healthy"] is False  # drift reported, not hidden


def test_correction_clamped_to_bound():
    true_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=30, pitch_deg=20)
    cfg_geo = CameraGeometry(position=(0, 0, 2.2), yaw_deg=55, pitch_deg=20)  # way off
    cal = AnchorCalibrator(cfg_geo, ANCHOR, auto_correct=True, max_correction_deg=10.0)
    for i in range(80):
        pix = true_geo.world_to_pixel(ANCHOR["aruco:100"])
        cal.observe("aruco:100", pix[0], pix[1], float(i))
    # clamped at 10 deg from the configured pose; remaining error stays visible
    assert math.degrees(cfg_geo.yaw) == pytest.approx(45.0, abs=0.1)
    s = cal.status()
    assert s["correction_yaw_deg"] == pytest.approx(-10.0, abs=0.1)
    assert s["healthy"] is False


# --- twin strips: two markers, one ID ------------------------------------------

# strip mounted perpendicular to the camera's line of sight, 140 mm apart
TWIN_LEFT = (4.0, 1.93, 0.9)
TWIN_RIGHT = (4.0, 2.07, 0.9)
TWIN_CENTER = (4.0, 2.0, 0.9)


def test_single_point_anchor_biased_by_twin_occlusion():
    """Documents the failure mode: a twin strip surveyed as ONE center point
    pulls the pose wrong when only one end is visible."""
    true_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=0, pitch_deg=18)
    cfg_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=0, pitch_deg=18)
    cal = AnchorCalibrator(cfg_geo, {"aruco:100": TWIN_CENTER}, auto_correct=True)
    for i in range(60):  # right end occluded: camera only ever sees the left marker
        pix = true_geo.world_to_pixel(TWIN_LEFT)
        cal.observe("aruco:100", pix[0], pix[1], float(i))
    # ~atan(0.07 / 4.0) ~ 1.0 deg of yaw bias from a perfectly mounted camera
    assert abs(math.degrees(cfg_geo.yaw)) > 0.8


def test_multi_position_anchor_immune_to_twin_occlusion():
    true_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=0, pitch_deg=18)
    cfg_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=0, pitch_deg=18)
    cal = AnchorCalibrator(
        cfg_geo, {"aruco:100": [TWIN_LEFT, TWIN_RIGHT]}, auto_correct=True
    )
    for i in range(60):  # only the left marker visible
        pix = true_geo.world_to_pixel(TWIN_LEFT)
        cal.observe("aruco:100", pix[0], pix[1], float(i))
    s = cal.status()
    assert abs(math.degrees(cfg_geo.yaw)) < 0.01
    assert s["residual_deg"] < 0.05 and s["healthy"] is True


def test_multi_position_anchor_corrects_drift_with_both_ends():
    """Both markers visible each frame AND the camera is bumped: each
    sighting matches its own hypothesis and the drift still heals."""
    true_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=0, pitch_deg=18)
    cfg_geo = CameraGeometry(position=(0, 2.0, 2.2), yaw_deg=1.5, pitch_deg=18)
    cal = AnchorCalibrator(
        cfg_geo, {"aruco:100": [TWIN_LEFT, TWIN_RIGHT]}, auto_correct=True
    )
    for i in range(80):
        for marker in (TWIN_LEFT, TWIN_RIGHT):
            pix = true_geo.world_to_pixel(marker)
            cal.observe("aruco:100", pix[0], pix[1], float(i))
    assert math.degrees(cfg_geo.yaw) == pytest.approx(0.0, abs=0.06)
    assert cal.status()["healthy"] is True


def test_config_twin_tag_positions(tmp_path):
    from apartment_tracker.config import load_config

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        """
world:
  zones: []
  spots:
    - name: shelf-b3
      tag: "aruco:14"
      position: [4.0, 2.0, 0.9]
      tag_positions: [[3.93, 2.0, 0.9], [4.07, 2.0, 0.9]]
  anchors:
    - {tag: "aruco:100", positions: [[0.0, 0.0, 1.0], [0.2, 0.0, 1.0]]}
items: []
sensors: []
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.anchors["aruco:14"] == [(3.93, 2.0, 0.9), (4.07, 2.0, 0.9)]
    assert cfg.anchors["aruco:100"] == [(0.0, 0.0, 1.0), (0.2, 0.0, 1.0)]


class AnchorAndItemDetector:
    def detect(self, frame):
        return [
            Detection(label="aruco", confidence=1.0, bbox=(0.5, 0.5, 0, 0), tag_id="aruco:100"),
            Detection(label="aruco", confidence=1.0, bbox=(0.4, 0.6, 0, 0), tag_id="aruco:7"),
        ]


def test_camera_consumes_anchor_detections():
    geo = CameraGeometry(position=(1, 1, 2.0), yaw_deg=0, pitch_deg=60)
    cam = CameraSensor(
        "cam", geometry=geo, frame_source=StaticFrameSource([object()]),
        detector=AnchorAndItemDetector(), surface_z=0.0,
    )
    cam.attach_anchors({"aruco:100": (3.0, 1.0, 0.0)})
    obs = cam.poll()
    # the anchor became calibration input, the item passed through
    assert len(obs) == 1
    assert isinstance(obs[0], PositionObservation) and obs[0].item_id == "aruco:7"
    assert cam.calibrator.status()["anchors"] == ["aruco:100"]
    assert cam.overlay()["calibration"] is not None


def test_anchors_optional_without_config():
    geo = CameraGeometry(position=(1, 1, 2.0), yaw_deg=0, pitch_deg=60)
    cam = CameraSensor(
        "cam", geometry=geo, frame_source=StaticFrameSource([object()]),
        detector=AnchorAndItemDetector(), surface_z=0.0,
    )
    cam.attach_anchors({})  # no anchors configured: everything passes through
    assert cam.calibrator is None
    assert len(cam.poll()) == 2
    assert cam.overlay()["calibration"] is None


def test_simulation_bumped_camera_self_heals():
    """The kitchen camera config is 1.5 deg off; the counter anchor fixes it
    and the keys stay accurately placed."""
    tracker, state = build_simulation(seed=4)
    for _ in range(80):
        state.tick()
        tracker.step()
    cam = next(s for s in tracker.sensors if s.sensor_id == "cam-kitchen")
    cal = cam.calibrator.status()
    assert cal["healthy"] is True
    assert cal["correction_yaw_deg"] == pytest.approx(-1.5, abs=0.2)
    assert math.degrees(cam.geometry.yaw) == pytest.approx(CAM_KITCHEN_YAW_TRUE, abs=0.2)
    snap = {e["item_id"]: e for e in tracker.engine.snapshot(now=state.now)}
    assert snap["keys"]["zone"] == "kitchen_counter"


def test_anchor_in_map_overlay():
    from apartment_tracker.overlay import map_overlay

    tracker, state = build_simulation(seed=4)
    for _ in range(10):
        state.tick()
        tracker.step()
    d = map_overlay(tracker)
    assert d["anchors"] == [{"tag": ANCHOR_TAG, "position": [6.9, 1.5, 0.9]}]
    kitchen_cam = next(c for c in d["cameras"] if c["sensor_id"] == "cam-kitchen")
    assert kitchen_cam["calibration"]["anchors"] == [ANCHOR_TAG]


def test_config_attaches_anchors(tmp_path):
    from apartment_tracker import registry
    from apartment_tracker.config import load_config

    registry.load_plugins()

    @registry.register("detector", "anchor_test_dummy")
    class Dummy:
        def detect(self, frame):
            return []

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        """
world:
  zones: []
  anchors:
    - {tag: "aruco:100", position: [1.0, 2.0, 0.9]}
items: []
sensors:
  - type: camera
    id: cam-1
    position: [0, 0, 2.2]
    yaw_deg: 30
    pitch_deg: 20
    anchor_correct: true
    source: {type: static}
    detector: {type: anchor_test_dummy}
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.anchors == {"aruco:100": [(1.0, 2.0, 0.9)]}
    cam = cfg.sensors[0]
    assert cam.calibrator is not None
    assert cam.calibrator.auto_correct is True
