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


def _camera_world_rotation(geometry):
    """World->camera rotation rows (right, down, forward) for our pinhole
    convention — shared by locate_board."""
    import numpy as np

    sy, cy = math.sin(geometry.yaw), math.cos(geometry.yaw)
    sp, cp = math.sin(geometry.pitch), math.cos(geometry.pitch)
    right = (sy, -cy, 0.0)
    down = (-sp * cy, -sp * sy, -cp)
    forward = (cp * cy, cp * sy, -sp)
    return np.array([right, down, forward], dtype=np.float64)


def locate_board(
    detections: dict[int, list[tuple[float, float]]],
    geometry,
    image_size: tuple[int, int],
    hfov_deg: float,
    scale: float = 1.0,
    known_surface_z: float | None = None,
) -> dict:
    """The inverse instrument: a CALIBRATED camera measures the board.

    World-coordinate agnostic — lay the sheet anywhere (counter, shelf,
    floor) and get back where it is: board origin in world, the surface
    plane z, tilt off horizontal — and, when `known_surface_z` is given
    (floor = 0.0, a counter you've measured once), a print-scale check:
    monocular planar scale is otherwise unobservable (the claimed size
    cancels exactly — verified during development), but a wrong print
    scale slides the solved board along the view ray, so the depth ratio
    to the known surface recovers it and suggests the true --marker-mm.
    """
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError(
            "pose bootstrap requires opencv: pip install hometwin[vision]"
        ) from e
    from hometwin.board import board_layout, board_object_points

    ids = sorted(detections)
    if not ids:
        raise ValueError("no board markers detected")
    obj = np.array(
        [pt for quad in board_object_points(ids, scale) for pt in quad],
        dtype=np.float64,
    )
    img = np.ascontiguousarray(
        np.array([pt for mid in ids for pt in detections[mid]], dtype=np.float64)
    ).reshape(-1, 1, 2)
    w, h = image_size
    fx = w / (2.0 * math.tan(math.radians(hfov_deg) / 2.0))
    K = np.array([[fx, 0, w / 2.0], [0, fx, h / 2.0], [0, 0, 1]], dtype=np.float64)

    solutions = []
    try:
        _, rvecs, tvecs, _ = cv2.solvePnPGeneric(obj, img, K, None,
                                                 flags=cv2.SOLVEPNP_IPPE)
        solutions += list(zip(rvecs, tvecs))
    except cv2.error:
        pass
    ok, rvec_i, tvec_i = cv2.solvePnP(obj, img, K, None, flags=cv2.SOLVEPNP_ITERATIVE)
    if ok:
        solutions.append((rvec_i, tvec_i))
    if not solutions:
        raise ValueError("PnP solve failed — board corners degenerate?")

    R_wc = _camera_world_rotation(geometry)
    C = np.array(geometry.position, dtype=np.float64)
    best = None
    for rvec, tvec in solutions:
        R_cb, _ = cv2.Rodrigues(rvec)  # x_cam = R_cb x_board + t
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, None)
        err = float(np.linalg.norm(proj.reshape(-1, 2) - img.reshape(-1, 2), axis=1).mean())
        origin_w = C + R_wc.T @ tvec.flatten()
        axes_w = R_wc.T @ R_cb  # board axes as world columns
        # a real sheet lies on a surface below the camera, normal up-ish
        plausible = origin_w[2] < C[2] and axes_w[2, 2] > 0
        cand = (err, origin_w, axes_w, plausible)
        if best is None or (plausible and not best[3]) or (
            plausible == best[3] and err < best[0]
        ):
            best = cand
    err, origin_w, axes_w, _ = best

    tilt = math.degrees(math.acos(max(min(float(axes_w[2, 2]), 1.0), -1.0)))
    board_yaw = math.degrees(math.atan2(float(axes_w[1, 0]), float(axes_w[0, 0])))
    out = {
        "origin_world": [round(float(v), 3) for v in origin_w],
        "surface_z": round(float(origin_w[2]), 3),
        "board_yaw_deg": round(board_yaw, 2),
        "tilt_deg": round(tilt, 2),
        "reprojection_error_px": round(err, 3),
        "markers_used": ids,
        "marker_positions_world": {},
        "scale_ratio": None,
        "suggested_marker_mm": None,
    }
    layout = board_layout(scale)
    for mid in ids:
        cx, cy2 = layout["centers"][mid]
        pos = origin_w + axes_w @ np.array([cx, cy2, 0.0])
        out["marker_positions_world"][mid] = [round(float(v), 3) for v in pos]

    if known_surface_z is not None:
        cam_drop = float(C[2]) - known_surface_z
        solved_drop = float(C[2]) - float(origin_w[2])
        if abs(solved_drop) > 0.05 and abs(cam_drop) > 0.05:
            # wrong print scale slides the board along the view ray: the
            # depth ratio against the known surface recovers the true scale
            ratio = cam_drop / solved_drop
            out["scale_ratio"] = round(ratio, 4)
            from hometwin.board import MARKER_MM

            out["suggested_marker_mm"] = round(MARKER_MM * scale * ratio, 1)
    return out


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
