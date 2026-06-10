"""Automatic camera-pose calibration from a COLMAP reconstruction.

The SfM step of every Gaussian-splat pipeline (COLMAP, or the COLMAP export
of Polycam/nerfstudio) localizes every photo in the scan — including
snapshots taken by the apartment's fixed cameras, if they were added to the
image set. This module turns those COLMAP poses into ready-to-paste camera
config (position / yaw_deg / pitch_deg / hfov_deg), replacing tape-measure
extrinsics.

The reconstruction lives in an arbitrary frame; a 2D-similarity + height
alignment (gravity-aligned scans only, which phone scan apps produce) maps
it onto the world frame from >= 2 reference points with known world
coordinates (e.g. two ArUco markers on the floor whose positions appear in
`world.zones` planning).

COLMAP text-model conventions: x_cam = R(qvec) @ x_world + tvec, camera
center C = -R^T @ tvec, viewing direction R row 2 (camera +z).
"""

from __future__ import annotations

import math
from pathlib import Path

Vec3 = tuple[float, float, float]


def qvec_to_rotmat(q: tuple[float, float, float, float]) -> list[list[float]]:
    w, x, y, z = q
    n = math.sqrt(w * w + x * x + y * y + z * z)
    w, x, y, z = w / n, x / n, y / n, z / n
    return [
        [1 - 2 * y * y - 2 * z * z, 2 * x * y - 2 * z * w, 2 * x * z + 2 * y * w],
        [2 * x * y + 2 * z * w, 1 - 2 * x * x - 2 * z * z, 2 * y * z - 2 * x * w],
        [2 * x * z - 2 * y * w, 2 * y * z + 2 * x * w, 1 - 2 * x * x - 2 * y * y],
    ]


def rotmat_to_qvec(R: list[list[float]]) -> tuple[float, float, float, float]:
    tr = R[0][0] + R[1][1] + R[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return (s / 4, (R[2][1] - R[1][2]) / s, (R[0][2] - R[2][0]) / s, (R[1][0] - R[0][1]) / s)
    i = max(range(3), key=lambda k: R[k][k])
    j, k = (i + 1) % 3, (i + 2) % 3
    s = math.sqrt(max(1.0 + R[i][i] - R[j][j] - R[k][k], 0.0)) * 2
    q = [0.0, 0.0, 0.0, 0.0]
    q[0] = (R[k][j] - R[j][k]) / s
    q[i + 1] = s / 4
    q[j + 1] = (R[j][i] + R[i][j]) / s
    q[k + 1] = (R[k][i] + R[i][k]) / s
    return tuple(q)


def parse_cameras_txt(path: str | Path) -> dict[int, dict]:
    """COLMAP cameras.txt -> {camera_id: {hfov_deg, width, height}}."""
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        cam_id, model = int(parts[0]), parts[1]
        width = float(parts[2])
        fx = float(parts[4])  # first param is f/fx for all pinhole-family models
        out[cam_id] = {
            "model": model,
            "width": int(width),
            "height": int(parts[3]),
            "hfov_deg": math.degrees(2.0 * math.atan(width / (2.0 * fx))),
        }
    return out


def parse_images_txt(path: str | Path) -> list[dict]:
    """COLMAP images.txt -> [{name, qvec, tvec, camera_id}].

    Pose lines alternate with 2D-point lines, but the point lines may be
    empty — so identify pose lines structurally: 10 fields ending in a
    non-numeric image name (point lines are all-numeric triples).
    """
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 10:
            continue
        try:
            float(parts[9])
            continue  # 2D-point row
        except ValueError:
            pass
        out.append(
            {
                "name": parts[9],
                "qvec": tuple(float(v) for v in parts[1:5]),
                "tvec": tuple(float(v) for v in parts[5:8]),
                "camera_id": int(parts[8]),
            }
        )
    return out


_Y_UP_TO_Z_UP = [[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]]


def _apply(M: list[list[float]], v) -> Vec3:
    return tuple(sum(M[r][c] * v[c] for c in range(3)) for r in range(3))


def camera_pose(qvec, tvec, up_axis: str = "z") -> dict:
    """COLMAP pose -> our camera model: position + yaw/pitch (+ roll report)."""
    R = qvec_to_rotmat(qvec)
    Rt = list(zip(*R))
    center = tuple(-sum(Rt[r][c] * tvec[c] for c in range(3)) for r in range(3))
    forward = tuple(R[2])  # camera +z in world
    right = tuple(R[0])  # camera +x in world
    if up_axis == "y":
        center = _apply(_Y_UP_TO_Z_UP, center)
        forward = _apply(_Y_UP_TO_Z_UP, forward)
        right = _apply(_Y_UP_TO_Z_UP, right)
    yaw = math.atan2(forward[1], forward[0])
    pitch = math.asin(max(min(-forward[2], 1.0), -1.0))
    # our model has no roll; report it so significant tilt isn't silent
    roll = math.asin(max(min(right[2], 1.0), -1.0))
    return {
        "position": center,
        "yaw_deg": math.degrees(yaw),
        "pitch_deg": math.degrees(pitch),
        "roll_deg": math.degrees(roll),
    }


def fit_alignment(pairs: list[dict]) -> dict:
    """2D similarity (scale, rotation about z, translation) + z offset from
    >= 2 reference points: [{"colmap": [x,y,z], "world": [x,y,z]}].

    Assumes a gravity-aligned reconstruction (convert y-up first).
    """
    if len(pairs) < 2:
        raise ValueError("need at least 2 reference points")
    cs = [complex(p["colmap"][0], p["colmap"][1]) for p in pairs]
    ws = [complex(p["world"][0], p["world"][1]) for p in pairs]
    n = len(pairs)
    cm = sum(cs) / n
    wm = sum(ws) / n
    denom = sum(abs(c - cm) ** 2 for c in cs)
    if denom < 1e-12:
        raise ValueError("reference points must not be horizontally coincident")
    a = sum((w - wm) * (c - cm).conjugate() for w, c in zip(ws, cs)) / denom
    scale = abs(a)
    if scale < 1e-12:
        raise ValueError("degenerate alignment (zero scale)")
    t = wm - a * cm
    tz = sum(p["world"][2] - scale * p["colmap"][2] for p in pairs) / n
    residual = max(abs(a * c + t - w) for c, w in zip(cs, ws))
    return {
        "scale": scale,
        "yaw_deg": math.degrees(math.atan2(a.imag, a.real)),
        "tx": t.real,
        "ty": t.imag,
        "tz": tz,
        "residual_m": residual,
    }


def transform_point(p, align: dict) -> Vec3:
    a = align["scale"] * complex(
        math.cos(math.radians(align["yaw_deg"])), math.sin(math.radians(align["yaw_deg"]))
    )
    xy = a * complex(p[0], p[1]) + complex(align["tx"], align["ty"])
    return (xy.real, xy.imag, align["scale"] * p[2] + align["tz"])


def calibrate_cameras(
    colmap_dir: str | Path,
    pairs: list[dict],
    image_names: list[str] | None = None,
    up_axis: str = "z",
) -> dict:
    """Full pipeline: parse model, align to world frame, emit camera configs.

    image_names filters to the fixed-camera snapshots; default is every
    registered image (scan frames included, usually not what you want).
    """
    colmap_dir = Path(colmap_dir)
    cams = parse_cameras_txt(colmap_dir / "cameras.txt")
    images = parse_images_txt(colmap_dir / "images.txt")
    if up_axis == "y":
        pairs = [
            {**p, "colmap": list(_apply(_Y_UP_TO_Z_UP, p["colmap"]))} for p in pairs
        ]
    align = fit_alignment(pairs)
    out = []
    for img in images:
        if image_names is not None and img["name"] not in image_names:
            continue
        pose = camera_pose(img["qvec"], img["tvec"], up_axis=up_axis)
        pos = transform_point(pose["position"], align)
        entry = {
            "image": img["name"],
            "position": [round(v, 3) for v in pos],
            "yaw_deg": round((pose["yaw_deg"] + align["yaw_deg"] + 180.0) % 360.0 - 180.0, 2),
            "pitch_deg": round(pose["pitch_deg"], 2),
            "roll_deg": round(pose["roll_deg"], 2),
            "hfov_deg": round(cams[img["camera_id"]]["hfov_deg"], 2),
        }
        out.append(entry)
    return {"alignment": align, "cameras": out}
