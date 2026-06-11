"""Origin board: layout/SVG consistency and multi-marker pose recovery."""

import math

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from hometwin.board import (
    MARKER_IDS, MARKER_MM, SPACING_MM, board_layout, board_object_points, board_svg,
)
from hometwin.posefit import camera_pose_from_board
from hometwin.sensors.camera import CameraGeometry
from hometwin.tags import marker_bits

W, H = 1280, 960


def test_layout_and_object_points():
    layout = board_layout()
    assert layout["centers"][MARKER_IDS[0]] == (0.0, 0.0)
    assert layout["centers"][MARKER_IDS[1]] == (SPACING_MM / 1000.0, 0.0)
    # print-scale correction flows through every coordinate
    scaled = board_layout(scale=0.5)
    assert scaled["marker_size_m"] == pytest.approx(MARKER_MM / 2000.0)
    pts = board_object_points([MARKER_IDS[0]], scale=1.0)[0]
    assert pts[0] == pytest.approx((-0.03, 0.03, 0.0))  # TL of 60 mm marker
    with pytest.raises(KeyError):
        board_object_points([999])


def test_board_svg_contains_three_markers_and_axes():
    bits = {mid: marker_bits("DICT_4X4_250", mid) for mid in MARKER_IDS}
    svg = board_svg(bits)
    assert svg.count('fill="#ffffff"/>') >= 3  # three quiet-zone fields
    assert "+x" in svg and "+y" in svg and "ORIGIN BOARD" in svg
    with pytest.raises(KeyError):
        board_svg({MARKER_IDS[0]: bits[MARKER_IDS[0]]})


def project(geo, pts):
    out = []
    for p in pts:
        pix = geo.world_to_pixel(p)
        assert pix is not None
        out.append((pix[0] * W, pix[1] * H))
    return out


def detections_for(geo, ids, scale=1.0):
    return {
        mid: project(geo, quad)
        for mid, quad in zip(ids, board_object_points(ids, scale))
    }


def test_full_board_recovers_pose():
    true_geo = CameraGeometry(position=(0.3, -0.4, 0.5), yaw_deg=70, pitch_deg=30,
                              hfov_deg=70, aspect=W / H)
    pose = camera_pose_from_board(
        detections_for(true_geo, list(MARKER_IDS)), (W, H), 70.0)
    assert pose["position"] == pytest.approx((0.3, -0.4, 0.5), abs=0.01)
    assert pose["yaw_deg"] == pytest.approx(70.0, abs=0.3)
    assert pose["pitch_deg"] == pytest.approx(30.0, abs=0.3)
    assert pose["markers_used"] == sorted(MARKER_IDS)


def test_partial_board_still_solves():
    true_geo = CameraGeometry(position=(0.3, -0.4, 0.5), yaw_deg=70, pitch_deg=30,
                              hfov_deg=70, aspect=W / H)
    two = [MARKER_IDS[0], MARKER_IDS[2]]  # +x marker occluded
    pose = camera_pose_from_board(detections_for(true_geo, two), (W, H), 70.0)
    assert pose["position"] == pytest.approx((0.3, -0.4, 0.5), abs=0.02)


def test_print_scale_correction():
    """Printed at 80%: solving with the right scale recovers the camera."""
    true_geo = CameraGeometry(position=(0.2, -0.3, 0.6), yaw_deg=60, pitch_deg=40,
                              hfov_deg=70, aspect=W / H)
    dets = detections_for(true_geo, list(MARKER_IDS), scale=0.8)
    pose = camera_pose_from_board(dets, (W, H), 70.0, scale=0.8)
    assert pose["position"] == pytest.approx((0.2, -0.3, 0.6), abs=0.01)
    wrong = camera_pose_from_board(dets, (W, H), 70.0, scale=1.0)
    assert math.dist(wrong["position"], (0.2, -0.3, 0.6)) > 0.05  # scale matters


def test_board_from_real_detection():
    """Render the board markers into a frame; real cv2 detection -> pose."""
    geo = CameraGeometry(position=(0.055, 0.055, 0.6), yaw_deg=0, pitch_deg=90,
                         hfov_deg=70, aspect=W / H)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    frame = np.full((H, W), 255, dtype=np.uint8)
    for mid, quad in zip(MARKER_IDS, board_object_points(list(MARKER_IDS))):
        px = np.array(project(geo, quad), dtype=np.float32)
        marker = cv2.aruco.generateImageMarker(dictionary, mid, 200)
        src = np.array([[0, 0], [199, 0], [199, 199], [0, 199]], dtype=np.float32)
        M = cv2.getPerspectiveTransform(src, px)
        warped = cv2.warpPerspective(marker, M, (W, H), borderValue=255,
                                     flags=cv2.INTER_NEAREST)
        frame = np.minimum(frame, warped)
    corners, ids, _ = cv2.aruco.ArucoDetector(
        dictionary, cv2.aruco.DetectorParameters()
    ).detectMarkers(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
    assert ids is not None and set(ids.flatten()) >= set(MARKER_IDS)
    dets = {int(m): [tuple(pt) for pt in q[0]]
            for q, m in zip(corners, ids.flatten()) if int(m) in MARKER_IDS}
    pose = camera_pose_from_board(dets, (W, H), 70.0)
    assert pose["position"] == pytest.approx((0.055, 0.055, 0.6), abs=0.02)
    assert pose["pitch_deg"] == pytest.approx(90.0, abs=1.0)


# --- locate_board: the world-agnostic instrument ---------------------------------

def make_calibrated_cam():
    return CameraGeometry(position=(1.5, 0.2, 1.4), yaw_deg=75, pitch_deg=35,
                          hfov_deg=70, aspect=W / H)


def board_world_corners(origin, yaw_deg, ids):
    """Project board markers placed at an arbitrary world pose."""
    import numpy as np

    yaw = math.radians(yaw_deg)
    c, s = math.cos(yaw), math.sin(yaw)
    out = {}
    for mid, quad in zip(ids, board_object_points(list(ids))):
        out[mid] = [
            (origin[0] + px * c - py * s, origin[1] + px * s + py * c, origin[2])
            for px, py, _ in quad
        ]
    return out


def test_locate_board_measures_arbitrary_surface():
    from hometwin.posefit import locate_board

    geo = make_calibrated_cam()
    origin, byaw = (2.1, 1.3, 0.92), 25.0   # board on a counter, rotated
    world = board_world_corners(origin, byaw, MARKER_IDS)
    dets = {mid: project(geo, corners) for mid, corners in world.items()}
    r = locate_board(dets, geo, (W, H), 70.0)
    assert r["origin_world"] == pytest.approx(list(origin), abs=0.02)
    assert r["surface_z"] == pytest.approx(0.92, abs=0.02)
    assert r["board_yaw_deg"] == pytest.approx(25.0, abs=0.5)
    assert r["tilt_deg"] < 0.5
    assert r["scale_ratio"] is None  # scale needs a known surface (see below)
    # markers come back at their true world spots: ready-made anchors
    for mid, pos in r["marker_positions_world"].items():
        cx, cy = {MARKER_IDS[0]: (0, 0), MARKER_IDS[1]: (0.11, 0),
                  MARKER_IDS[2]: (0, 0.11)}[mid]
        c, s = math.cos(math.radians(byaw)), math.sin(math.radians(byaw))
        expected = (origin[0] + cx * c - cy * s, origin[1] + cx * s + cy * c, origin[2])
        assert pos == pytest.approx(list(expected), abs=0.02)


def test_locate_board_print_scale_via_known_surface():
    """Monocular planar scale is unobservable on its own — the claimed size
    cancels exactly (verified: without a constraint the ratio reads 1.0).
    Against a KNOWN surface height it becomes observable: a sheet printed
    at 80% but claimed 100% solves 25% too deep, and the depth ratio to
    the known floor recovers the true print scale."""
    from hometwin.board import MARKER_MM
    from hometwin.posefit import locate_board

    geo = make_calibrated_cam()
    origin = (2.1, 1.3, 0.0)  # board on the floor
    world = {}
    for mid, quad in zip(MARKER_IDS, board_object_points(list(MARKER_IDS), scale=0.8)):
        world[mid] = [(origin[0] + px, origin[1] + py, origin[2]) for px, py, _ in quad]
    dets = {mid: project(geo, corners) for mid, corners in world.items()}

    blind = locate_board(dets, geo, (W, H), 70.0, scale=1.0)
    assert blind["scale_ratio"] is None  # no constraint: honestly silent

    r = locate_board(dets, geo, (W, H), 70.0, scale=1.0, known_surface_z=0.0)
    assert r["scale_ratio"] == pytest.approx(0.8, abs=0.03)
    assert r["suggested_marker_mm"] == pytest.approx(MARKER_MM * 0.8, abs=2.0)
    # and a correctly-claimed board reads ~1.0
    ok = locate_board(dets, geo, (W, H), 70.0, scale=0.8, known_surface_z=0.0)
    assert ok["scale_ratio"] == pytest.approx(1.0, abs=0.03)


def test_runtime_anchor_drop_endpoint():
    import json as _json
    import urllib.request

    from hometwin.api import ApiServer
    from hometwin.simulate import build_simulation

    tracker, _ = build_simulation(seed=1)
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{api.port}/anchors",
            data=_json.dumps({"tag": "aruco:103", "position": [2.0, 1.0, 0.9]}).encode(),
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert _json.loads(r.read())["status"] == "anchored"
        assert (2.0, 1.0, 0.9) in tracker.cfg.anchors["aruco:103"]
        cam = next(s for s in tracker.sensors if s.sensor_id == "cam-kitchen")
        assert (2.0, 1.0, 0.9) in cam.calibrator.anchors["aruco:103"]
    finally:
        api.stop()
