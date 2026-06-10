"""Path traces: tracker history and overlay exposure."""

import urllib.request
import json

from apartment_tracker.api import ApiServer
from apartment_tracker.overlay import camera_overlay, map_overlay
from apartment_tracker.simulate import build_simulation


def run_sim(ticks=60, seed=3):
    tracker, state = build_simulation(seed=seed)
    for _ in range(ticks):
        state.tick()
        tracker.step()
    return tracker


def test_moving_item_leaves_trail():
    tracker = run_sim()
    assert "phone" in tracker.trails
    trail = list(tracker.trails["phone"])
    assert len(trail) >= 3
    # consecutive points are spaced by the minimum step
    import math
    for a, b in zip(trail, trail[1:]):
        assert math.dist(a["pos"], b["pos"]) >= 0.15
    # timestamps increase
    assert all(a["t"] <= b["t"] for a, b in zip(trail, trail[1:]))


def test_static_item_has_no_meaningful_trail():
    tracker = run_sim()
    keys_trail = tracker.trails.get("keys", [])
    assert len(keys_trail) <= 2  # initial convergence only, then stationary


def test_map_overlay_includes_trails():
    tracker = run_sim()
    d = map_overlay(tracker)
    trails = {t["item_id"]: t for t in d["trails"]}
    assert "phone" in trails
    assert len(trails["phone"]["points"]) >= 3
    assert all(len(p) == 3 for p in trails["phone"]["points"])


def test_camera_overlay_projects_trails():
    tracker = run_sim()
    d = camera_overlay(tracker, "cam-living-a")
    phone_segments = [t for t in d["trails"] if t["item_id"] == "phone"]
    assert phone_segments
    for seg in phone_segments:
        assert len(seg["points"]) >= 2


def test_trails_served_over_api():
    tracker = run_sim()
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{api.port}/overlay/map", timeout=5
        ) as r:
            d = json.loads(r.read())
        assert any(t["item_id"] == "phone" for t in d["trails"])
        assert "splat_transform" in d
    finally:
        api.stop()
