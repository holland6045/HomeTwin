"""Overlay assembly for the visualization UI.

Two render targets share the same layer data:
- map: top-down world view (zones, item tracks, BLE range rings, tomography
  heat map, presence, camera poses)
- camera: the same layers projected into one camera's pixel space, so the
  browser can composite them over the live video stream.

Everything here is plain JSON-serializable data; rendering happens
client-side (static/ui.html).
"""

from __future__ import annotations

import math
import os
import time

from hometwin.anchors import normalize_anchor_positions

HEAT_MIN_VALUE = 0.05  # skip near-zero cells to bound payload size
RING_SAMPLES = 36
ITEM_HEIGHT_M = 0.8  # range spheres are drawn where items live, not at the anchor


def _floorplan_meta_cached(tracker) -> dict | None:
    meta = getattr(tracker, "_floorplan_meta", "unset")
    if meta == "unset":
        from hometwin.floorplan import floorplan_meta

        meta = tracker._floorplan_meta = floorplan_meta(
            getattr(tracker.cfg, "floorplan", None)
        )
    return meta


def _splat_version(path: str | None) -> int | None:
    """mtime-based version so the UI reloads the scan after a hot swap."""
    if not path:
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def floor_ring(anchor: tuple, range_m: float, z: float = ITEM_HEIGHT_M) -> dict | None:
    """Horizontal circle where a range sphere intersects item height."""
    dz = anchor[2] - z
    r2 = range_m * range_m - dz * dz
    if r2 <= 0:
        return None
    return {"cx": anchor[0], "cy": anchor[1], "z": z, "radius": math.sqrt(r2)}


def map_overlay(tracker) -> dict:
    now = time.time()
    zones = [
        {"name": z.name, "min": list(z.min_corner), "max": list(z.max_corner)}
        for z in tracker.cfg.world.zones
    ]
    heatmaps, cameras = [], []
    for sensor in tracker.sensors:
        layer = sensor.overlay()
        if not layer:
            continue
        if layer["kind"] == "heatmap":
            heatmaps.append(layer)
        elif layer["kind"] == "camera":
            cameras.append(layer)
    st = tracker.overlay_state()  # locked copies: never iterate live structures
    rings = []
    for (sensor_id, item_id), r in st["ranges"].items():
        ring = floor_ring(r["anchor"], r["range_m"])
        if ring:
            rings.append(
                {
                    **ring,
                    "sensor_id": sensor_id,
                    "item_id": item_id,
                    "sigma_m": r["sigma_m"],
                    "age_s": round(now - r["timestamp"], 1),
                    "anchor": list(r["anchor"]),
                }
            )
    items = st["items"]
    positions = {e["item_id"]: e.get("position") for e in items}
    bearings = []
    for (sensor_id, item_id), b in st["bearings"].items():
        pos = positions.get(item_id)
        length = (
            math.dist(b["origin"], pos) * 1.15 if pos else 6.0
        )  # draw slightly past the estimate
        bearings.append(
            {
                "sensor_id": sensor_id,
                "item_id": item_id,
                "origin": list(b["origin"]),
                "direction": list(b["direction"]),
                "length_m": round(length, 2),
                "age_s": round(now - b["timestamp"], 1),
            }
        )
    trails = [
        {"item_id": item_id, "points": points}
        for item_id, points in st["trails"].items()
    ]
    anchors = [
        {"tag": tag, "position": list(pos)}
        for tag, positions in normalize_anchor_positions(
            getattr(tracker.cfg, "anchors", {})
        ).items()
        for pos in positions
    ]
    spots = [
        {"name": s.name, "position": list(s.position), "radius": s.radius, "tag": s.tag}
        for s in tracker.cfg.world.spots
    ]
    return {
        "timestamp": now,
        "zones": zones,
        "items": items,
        "rings": rings,
        "bearings": bearings,
        "trails": trails,
        "anchors": anchors,
        "spots": spots,
        "movables": (
            tracker.cfg.movables.snapshot() if getattr(tracker.cfg, "movables", None) else []
        ),
        "devices": [
            prof.as_dict()
            for sensor in tracker.sensors
            for prof in list(getattr(sensor, "profiles", {}).values())
        ],
        "device_tags": (
            tracker.cfg.device_tags.snapshot()
            if getattr(tracker.cfg, "device_tags", None)
            else []
        ),
        "learning": {
            "path_loss": tracker.learning_status() if hasattr(tracker, "learning_status") else [],
            "sensor_trust": st["sensor_trust"],
        },
        "heatmaps": heatmaps,
        "cameras": cameras,
        "presence": tracker.presence,
        "events": st["events"],
        "floorplan": _floorplan_meta_cached(tracker),
        "worldmodel": tracker.worldmodel.stats() if hasattr(tracker, "worldmodel") else None,
        "splat": bool(getattr(tracker.cfg, "splat_asset", None)),
        "splat_transform": getattr(tracker.cfg, "splat_transform", None),
        "splat_version": _splat_version(getattr(tracker.cfg, "splat_asset", None)),
    }


def _project_polyline(geo, points: list[tuple]) -> list[list[list[float]]]:
    """Project a world polyline; split into segments where it leaves view."""
    segments, current = [], []
    for p in points:
        pix = geo.world_to_pixel(p)
        if pix is None:
            if len(current) >= 2:
                segments.append(current)
            current = []
        else:
            current.append([round(pix[0], 4), round(pix[1], 4)])
    if len(current) >= 2:
        segments.append(current)
    return segments


def camera_overlay(tracker, sensor_id: str) -> dict | None:
    camera = next(
        (
            s
            for s in tracker.sensors
            if s.sensor_id == sensor_id and (s.overlay() or {}).get("kind") == "camera"
        ),
        None,
    )
    if camera is None:
        return None
    geo = camera.geometry
    now = time.time()

    st = tracker.overlay_state()
    items = []
    for entry in st["items"]:
        pos = entry.get("position")
        if pos is None:
            continue
        pix = geo.world_to_pixel(tuple(pos))
        if pix is None:
            continue
        side = geo.world_to_pixel((pos[0] + entry["sigma_m"], pos[1], pos[2]))
        sigma_px = abs(side[0] - pix[0]) if side else 0.02
        items.append(
            {
                "item_id": entry["item_id"],
                "name": entry["name"],
                "status": entry["status"],
                "u": round(pix[0], 4),
                "v": round(pix[1], 4),
                "sigma_u": round(sigma_px, 4),
                "age_s": entry["age_s"],
            }
        )

    heat = []
    for sensor in tracker.sensors:
        layer = sensor.overlay()
        if not layer or layer["kind"] != "heatmap" or not layer.get("values"):
            continue
        xmin, ymin, _, _ = layer["bounds"]
        cell, z = layer["cell_m"], layer["height_m"]
        for iy, row in enumerate(layer["values"]):
            for ix, value in enumerate(row):
                if value < HEAT_MIN_VALUE:
                    continue
                x0, y0 = xmin + ix * cell, ymin + iy * cell
                corners = [
                    geo.world_to_pixel(p)
                    for p in (
                        (x0, y0, z),
                        (x0 + cell, y0, z),
                        (x0 + cell, y0 + cell, z),
                        (x0, y0 + cell, z),
                    )
                ]
                if any(c is None for c in corners):
                    continue
                heat.append(
                    {"q": [[round(u, 4), round(v, 4)] for u, v in corners], "v": value}
                )

    rings = []
    for (rs_id, item_id), r in st["ranges"].items():
        ring = floor_ring(r["anchor"], r["range_m"])
        if ring is None:
            continue
        points = [
            (
                ring["cx"] + ring["radius"] * math.cos(2 * math.pi * i / RING_SAMPLES),
                ring["cy"] + ring["radius"] * math.sin(2 * math.pi * i / RING_SAMPLES),
                ring["z"],
            )
            for i in range(RING_SAMPLES + 1)
        ]
        for seg in _project_polyline(geo, points):
            rings.append({"sensor_id": rs_id, "item_id": item_id, "points": seg})

    zones = []
    for zone in tracker.cfg.world.zones:
        (x0, y0, z0), (x1, y1, _) = zone.min_corner, zone.max_corner
        outline = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z0)]
        for seg in _project_polyline(geo, outline):
            zones.append({"name": zone.name, "points": seg})

    trails = []
    for item_id, points in st["trails"].items():
        for seg in _project_polyline(geo, [tuple(p) for p in points]):
            trails.append({"item_id": item_id, "points": seg})

    presence = None
    if tracker.presence:
        pix = geo.world_to_pixel(tuple(tracker.presence["centroid"]))
        if pix is not None:
            presence = {
                "u": round(pix[0], 4),
                "v": round(pix[1], 4),
                "sigma_m": tracker.presence["sigma_m"],
                "zone": tracker.presence["zone"],
            }

    return {
        "timestamp": now,
        "sensor_id": sensor_id,
        "stream_url": camera.overlay().get("stream_url"),
        "items": items,
        "heat": heat,
        "rings": rings,
        "trails": trails,
        "zones": zones,
        "presence": presence,
    }
