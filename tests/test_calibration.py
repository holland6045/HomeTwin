"""COLMAP -> camera-pose calibration: synthetic reconstructions round-trip."""

import math

import pytest

from hometwin.calibration import (
    calibrate_cameras,
    camera_pose,
    fit_alignment,
    qvec_to_rotmat,
    rotmat_to_qvec,
    transform_point,
)


def rotmat_from_yaw_pitch(yaw_deg, pitch_deg):
    """World->cam rotation for our camera convention (x right, y down, z fwd)."""
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    forward = (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))
    right = (-math.sin(y), math.cos(y), 0.0)
    down = (
        forward[1] * right[2] - forward[2] * right[1],
        forward[2] * right[0] - forward[0] * right[2],
        forward[0] * right[1] - forward[1] * right[0],
    )
    return [list(right), list(down), list(forward)]


def colmap_pose(position, yaw_deg, pitch_deg):
    """Our pose -> COLMAP qvec/tvec (x_cam = R x_world + t, t = -R C)."""
    R = rotmat_from_yaw_pitch(yaw_deg, pitch_deg)
    t = tuple(-sum(R[r][c] * position[c] for c in range(3)) for r in range(3))
    return rotmat_to_qvec(R), t


def write_model(tmp_path, cameras, hfov_deg=80.0, width=1600, height=1200):
    fx = width / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
    (tmp_path / "cameras.txt").write_text(
        "# Camera list\n"
        f"1 PINHOLE {width} {height} {fx} {fx} {width / 2} {height / 2}\n"
    )
    lines = ["# Image list"]
    for i, (name, position, yaw, pitch) in enumerate(cameras, start=1):
        q, t = colmap_pose(position, yaw, pitch)
        lines.append(
            f"{i} {q[0]} {q[1]} {q[2]} {q[3]} {t[0]} {t[1]} {t[2]} 1 {name}"
        )
        lines.append("")  # 2D-points line (empty)
    (tmp_path / "images.txt").write_text("\n".join(lines) + "\n")


def test_qvec_rotmat_roundtrip():
    R = rotmat_from_yaw_pitch(-110.0, 40.0)
    R2 = qvec_to_rotmat(rotmat_to_qvec(R))
    for r in range(3):
        assert R2[r] == pytest.approx(R[r], abs=1e-9)


def test_camera_pose_recovers_yaw_pitch():
    q, t = colmap_pose((7.5, 3.8, 2.3), -110.0, 40.0)
    pose = camera_pose(q, t)
    assert pose["position"] == pytest.approx((7.5, 3.8, 2.3), abs=1e-9)
    assert pose["yaw_deg"] == pytest.approx(-110.0, abs=1e-6)
    assert pose["pitch_deg"] == pytest.approx(40.0, abs=1e-6)
    assert pose["roll_deg"] == pytest.approx(0.0, abs=1e-6)


def test_fit_alignment_recovers_similarity():
    truth = {"scale": 2.5, "yaw_deg": 35.0, "tx": 1.2, "ty": -0.7, "tz": 0.4}
    world_pts = [(0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 3.0, 0.9), (1.0, 2.0, 0.0)]

    def world_to_colmap(p):
        # invert the similarity to synthesize colmap coordinates
        a = truth["scale"] * complex(
            math.cos(math.radians(truth["yaw_deg"])), math.sin(math.radians(truth["yaw_deg"]))
        )
        xy = (complex(p[0], p[1]) - complex(truth["tx"], truth["ty"])) / a
        return [xy.real, xy.imag, (p[2] - truth["tz"]) / truth["scale"]]

    pairs = [{"colmap": world_to_colmap(p), "world": list(p)} for p in world_pts]
    fit = fit_alignment(pairs)
    for key in ("scale", "yaw_deg", "tx", "ty", "tz"):
        assert fit[key] == pytest.approx(truth[key], abs=1e-9)
    assert fit["residual_m"] < 1e-9
    for p in world_pts:
        assert transform_point(world_to_colmap(p), fit) == pytest.approx(p, abs=1e-9)


def test_fit_alignment_rejects_degenerate():
    with pytest.raises(ValueError):
        fit_alignment([{"colmap": [0, 0, 0], "world": [1, 1, 0]}])
    with pytest.raises(ValueError):
        fit_alignment(
            [
                {"colmap": [1, 1, 0], "world": [0, 0, 0]},
                {"colmap": [1, 1, 5], "world": [2, 0, 0]},
            ]
        )


def test_calibrate_cameras_end_to_end(tmp_path):
    """Cameras posed in an arbitrary scan frame come back in world coords."""
    truth_cams = [
        ("cam-kitchen.jpg", (7.5, 3.8, 2.3), -110.0, 40.0),
        ("cam-living-a.jpg", (0.0, 0.0, 2.4), 50.0, 25.0),
    ]
    # the scan frame is rotated/scaled/offset relative to the world frame
    scan = {"scale": 1.8, "yaw_deg": -25.0, "tx": 3.0, "ty": 1.5, "tz": -0.2}
    a = scan["scale"] * complex(
        math.cos(math.radians(scan["yaw_deg"])), math.sin(math.radians(scan["yaw_deg"]))
    )

    def world_to_scan(p):
        xy = (complex(p[0], p[1]) - complex(scan["tx"], scan["ty"])) / a
        return (xy.real, xy.imag, (p[2] - scan["tz"]) / scan["scale"])

    scan_cams = [
        (name, world_to_scan(pos), yaw - scan["yaw_deg"], pitch)
        for name, pos, yaw, pitch in truth_cams
    ]
    write_model(tmp_path, scan_cams, hfov_deg=80.0)

    ref_world = [(0.0, 0.0, 0.0), (8.0, 0.0, 0.0), (8.0, 4.0, 0.9)]
    pairs = [{"colmap": list(world_to_scan(p)), "world": list(p)} for p in ref_world]

    result = calibrate_cameras(tmp_path, pairs)
    assert result["alignment"]["residual_m"] < 1e-6
    by_name = {c["image"]: c for c in result["cameras"]}
    for name, pos, yaw, pitch in truth_cams:
        cam = by_name[name]
        assert cam["position"] == pytest.approx(list(pos), abs=1e-2)
        assert cam["yaw_deg"] == pytest.approx(yaw, abs=0.05)
        assert cam["pitch_deg"] == pytest.approx(pitch, abs=0.05)
        assert cam["hfov_deg"] == pytest.approx(80.0, abs=0.05)


def test_calibrate_filters_to_requested_images(tmp_path):
    write_model(tmp_path, [
        ("cam-1.jpg", (1.0, 1.0, 2.0), 10.0, 30.0),
        ("scan-frame-001.jpg", (2.0, 2.0, 1.5), 90.0, 5.0),
    ])
    pairs = [
        {"colmap": [0, 0, 0], "world": [0, 0, 0]},
        {"colmap": [1, 0, 0], "world": [1, 0, 0]},
    ]
    result = calibrate_cameras(tmp_path, pairs, image_names=["cam-1.jpg"])
    assert [c["image"] for c in result["cameras"]] == ["cam-1.jpg"]


def test_y_up_scan_converted(tmp_path):
    """A y-up reconstruction (common phone-app export) calibrates correctly."""
    # camera at world (2, 3, 2.0) looking yaw=0 pitch=30 in a z-up world;
    # the same scene in y-up coords: (x, y, z)_zup -> (x, z, -y)_yup
    R_zup = rotmat_from_yaw_pitch(0.0, 30.0)
    M = [[1, 0, 0], [0, 0, -1], [0, 1, 0]]  # y-up -> z-up (module convention)
    Minv = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    C_zup = (2.0, 3.0, 2.0)
    C_yup = tuple(sum(Minv[r][c] * C_zup[c] for c in range(3)) for r in range(3))
    # R maps world->cam; in y-up coordinates R' = R_zup @ M
    R_yup = [[sum(R_zup[r][k] * M[k][c] for k in range(3)) for c in range(3)] for r in range(3)]
    t = tuple(-sum(R_yup[r][c] * C_yup[c] for c in range(3)) for r in range(3))
    pose = camera_pose(rotmat_to_qvec(R_yup), t, up_axis="y")
    assert pose["position"] == pytest.approx(C_zup, abs=1e-9)
    assert pose["yaw_deg"] == pytest.approx(0.0, abs=1e-6)
    assert pose["pitch_deg"] == pytest.approx(30.0, abs=1e-6)
