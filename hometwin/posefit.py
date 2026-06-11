"""Single-marker camera pose bootstrap (PnP).

A printed ArUco marker of known physical size, laid flat at a known world
position (e.g. the world origin on the desk, top edge pointing +y), shows
four corners to the camera — enough for a full 6-DOF PnP solve. That turns
first-run setup from "measure your camera's position and angles" into
"put the tag on the desk and run `hometwin webcam-setup`".

Requires opencv (vision extra); the conversion to our camera convention is
shared with the COLMAP calibration path.
"""

from __future__ import annotations

import math

from hometwin.calibration import camera_pose, rotmat_to_qvec


def marker_corners_world(
    position: tuple[float, float, float], size_m: float, yaw_deg: float = 0.0
) -> list[tuple[float, float, float]]:
    """World coordinates of a flat marker's corners in ArUco order
    (top-left, top-right, bottom-right, bottom-left), top edge toward +y
    when yaw_deg = 0."""
    h = size_m / 2.0
    local = [(-h, h), (h, h), (h, -h), (-h, -h)]
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    return [
        (position[0] + x * c - y * s, position[1] + x * s + y * c, position[2])
        for x, y in local
    ]


def camera_pose_from_marker(
    corners_px: list[tuple[float, float]],
    image_size: tuple[int, int],
    hfov_deg: float,
    marker_position: tuple[float, float, float],
    marker_size_m: float,
    marker_yaw_deg: float = 0.0,
) -> dict:
    """Solve the camera pose from one marker detection.

    corners_px: pixel coordinates in ArUco corner order.
    Returns position / yaw_deg / pitch_deg / roll_deg in our convention.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError(
            "pose bootstrap requires opencv: pip install hometwin[vision]"
        ) from e
    w, h = image_size
    fx = w / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
    K = np.array([[fx, 0, w / 2.0], [0, fx, h / 2.0], [0, 0, 1]], dtype=np.float64)
    # IPPE requires the plane z=0: solve in marker-local coordinates, then
    # compose with the marker's world pose
    half = marker_size_m / 2.0
    local = np.array(
        [[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]],
        dtype=np.float64,
    )
    img = np.ascontiguousarray(np.array(corners_px, dtype=np.float64)).reshape(-1, 1, 2)
    # IPPE yields both planar-ambiguity solutions but degrades on frontal
    # (straight-down) views; ITERATIVE nails frontal but returns only one
    # solution. Pool all candidates and pick by physics + reprojection.
    solutions = []
    n_sol, rvecs, tvecs, _ = cv2.solvePnPGeneric(
        local, img, K, None, flags=cv2.SOLVEPNP_IPPE
    )
    solutions += list(zip(rvecs, tvecs))
    ok, rvec_i, tvec_i = cv2.solvePnP(local, img, K, None, flags=cv2.SOLVEPNP_ITERATIVE)
    if ok:
        solutions.append((rvec_i, tvec_i))
    if not solutions:
        raise ValueError("PnP solve failed — marker corners degenerate?")
    myaw = math.radians(marker_yaw_deg)
    cy, sy = math.cos(myaw), math.sin(myaw)
    R_wm = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=np.float64)
    p_m = np.array(marker_position, dtype=np.float64)
    candidates = []
    for rvec, tvec in solutions:
        R_lm, _ = cv2.Rodrigues(rvec)
        proj, _ = cv2.projectPoints(local, rvec, tvec, K, None)
        err = float(np.linalg.norm(proj.reshape(-1, 2) - img.reshape(-1, 2), axis=1).mean())
        R_w = R_lm @ R_wm.T
        t_w = tvec.flatten() - R_w @ p_m
        pose = camera_pose(rotmat_to_qvec(R_w.tolist()), tuple(t_w))
        candidates.append((err, pose))
    # planar PnP is two-fold ambiguous; the camera looking at a flat tag on a
    # surface is above that surface — discard solutions that aren't
    above = [c for c in candidates if c[1]["position"][2] > marker_position[2]]
    err, pose = min(above or candidates, key=lambda c: c[0])
    pose["hfov_deg"] = hfov_deg
    pose["reprojection_error_px"] = round(err, 3)
    return pose


def camera_pose_from_board(
    detections: dict[int, list[tuple[float, float]]],
    image_size: tuple[int, int],
    hfov_deg: float,
    scale: float = 1.0,
    board_z: float = 0.0,
) -> dict:
    """Solve the camera pose from origin-board detections.

    detections: {marker_id: 4 corner pixels in ArUco order} for any subset
    of the board's markers — one marker works, three give 12 correspondences
    and an unambiguous frame. The board defines the world: its origin marker
    center is (0, 0, board_z).
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError(
            "pose bootstrap requires opencv: pip install hometwin[vision]"
        ) from e
    from hometwin.board import board_object_points

    ids = sorted(detections)
    if not ids:
        raise ValueError("no board markers detected")
    obj = np.array(
        [pt for quad in board_object_points(ids, scale) for pt in quad],
        dtype=np.float64,
    )  # z = 0 plane: IPPE-friendly as-is
    img = np.ascontiguousarray(
        np.array([pt for mid in ids for pt in detections[mid]], dtype=np.float64)
    ).reshape(-1, 1, 2)
    w, h = image_size
    fx = w / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
    K = np.array([[fx, 0, w / 2.0], [0, fx, h / 2.0], [0, 0, 1]], dtype=np.float64)

    solutions = []
    try:
        n_sol, rvecs, tvecs, _ = cv2.solvePnPGeneric(
            obj, img, K, None, flags=cv2.SOLVEPNP_IPPE
        )
        solutions += list(zip(rvecs, tvecs))
    except cv2.error:
        pass
    ok, rvec_i, tvec_i = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_ITERATIVE)
    if ok:
        solutions.append((rvec_i, tvec_i))
    if not solutions:
        raise ValueError("PnP solve failed — board corners degenerate?")
    candidates = []
    for rvec, tvec in solutions:
        R, _ = cv2.Rodrigues(rvec)
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, None)
        err = float(np.linalg.norm(proj.reshape(-1, 2) - img.reshape(-1, 2), axis=1).mean())
        # board plane sits at board_z in world: shift the translation
        t_w = tvec.flatten() - R @ np.array([0.0, 0.0, board_z])
        pose = camera_pose(rotmat_to_qvec(R.tolist()), tuple(t_w))
        candidates.append((err, pose))
    above = [c for c in candidates if c[1]["position"][2] > board_z]
    err, pose = min(above or candidates, key=lambda c: c[0])
    pose["hfov_deg"] = hfov_deg
    pose["reprojection_error_px"] = round(err, 3)
    pose["markers_used"] = ids
    return pose


def average_poses(poses: list[dict]) -> dict:
    """Average several single-frame solves (angles via vector mean)."""
    n = len(poses)
    out = {"position": tuple(sum(p["position"][i] for p in poses) / n for i in range(3))}
    for key in ("yaw_deg", "pitch_deg", "roll_deg"):
        sx = sum(math.cos(math.radians(p[key])) for p in poses)
        sy = sum(math.sin(math.radians(p[key])) for p in poses)
        out[key] = math.degrees(math.atan2(sy, sx))
    out["hfov_deg"] = poses[0].get("hfov_deg")
    return out
