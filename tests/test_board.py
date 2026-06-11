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
