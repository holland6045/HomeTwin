import json
import urllib.request

import pytest

from apartment_tracker.api import ApiServer
from apartment_tracker.overlay import camera_overlay, floor_ring, map_overlay
from apartment_tracker.sensors.camera import CameraGeometry
from apartment_tracker.simulate import build_simulation


def run_sim(ticks=40, seed=3):
    tracker, state = build_simulation(seed=seed)
    for _ in range(ticks):
        state.tick()
        tracker.step()
    return tracker


# --- projection --------------------------------------------------------------

def test_world_to_pixel_inverts_project_to_plane():
    geo = CameraGeometry(position=(7.5, 3.8, 2.3), yaw_deg=-110, pitch_deg=40, hfov_deg=80)
    for u, v in ((0.5, 0.5), (0.25, 0.7), (0.8, 0.6)):
        p = geo.project_to_plane(u, v, 0.9)
        assert p is not None
        pix = geo.world_to_pixel(p)
        assert pix == pytest.approx((u, v), abs=1e-9)


def test_world_to_pixel_behind_camera_is_none():
    geo = CameraGeometry(position=(0, 0, 2.0), yaw_deg=0, pitch_deg=20)
    assert geo.world_to_pixel((-5.0, 0.0, 1.0)) is None


def test_floor_ring_geometry():
    ring = floor_ring((0.0, 0.0, 2.2), 2.0, z=0.8)
    assert ring["radius"] == pytest.approx((2.0**2 - 1.4**2) ** 0.5)
    assert floor_ring((0.0, 0.0, 2.2), 1.0, z=0.8) is None  # sphere never reaches item height


# --- map overlay ---------------------------------------------------------------

def test_map_overlay_has_all_layers():
    tracker = run_sim()
    d = map_overlay(tracker)
    assert {z["name"] for z in d["zones"]} >= {"living_room", "kitchen"}
    assert {i["item_id"] for i in d["items"]} == {"keys", "wallet", "phone"}
    # BLE rings for wallet and phone from 3 scanners each
    ring_items = {r["item_id"] for r in d["rings"]}
    assert ring_items == {"wallet", "phone"}
    assert len(d["rings"]) == 6
    # tomography heat map present with normalized values
    assert len(d["heatmaps"]) == 1
    hm = d["heatmaps"][0]
    assert hm["values"] is not None
    peak = max(max(row) for row in hm["values"])
    assert peak == pytest.approx(1.0, abs=0.01)
    assert hm["nodes"]  # mesh node positions for drawing
    # camera pose layer
    assert d["cameras"][0]["sensor_id"] == "cam-kitchen"
    assert d["presence"]["zone"] == "living_room"


# --- camera overlay -----------------------------------------------------------

def test_camera_overlay_projects_items_and_zones():
    tracker = run_sim()
    d = camera_overlay(tracker, "cam-kitchen")
    assert d is not None
    items = {i["item_id"]: i for i in d["items"]}
    # the keys are on the counter in front of this camera
    assert "keys" in items
    assert 0.0 <= items["keys"]["u"] <= 1.0 and 0.0 <= items["keys"]["v"] <= 1.0
    assert items["keys"]["sigma_u"] >= 0.0
    # zone outlines projected as segments
    assert any(z["name"] == "kitchen_counter" for z in d["zones"])
    for z in d["zones"]:
        assert len(z["points"]) >= 2
    # BLE rings appear as projected polylines
    assert any(r["item_id"] == "wallet" for r in d["rings"])


def test_camera_overlay_unknown_camera():
    tracker = run_sim(ticks=5)
    assert camera_overlay(tracker, "no-such-cam") is None


def test_heat_quads_projected_when_visible():
    tracker = run_sim()
    d = camera_overlay(tracker, "cam-kitchen")
    # the living-room heat map may or may not fall inside this camera's view;
    # every emitted quad must be well-formed either way
    for cell in d["heat"]:
        assert len(cell["q"]) == 4
        assert 0.0 < cell["v"] <= 1.0


# --- API + UI ----------------------------------------------------------------

def test_api_serves_overlays_and_ui():
    tracker = run_sim()
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        base = f"http://127.0.0.1:{api.port}"
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            d = json.loads(r.read())
        assert d["heatmaps"] and d["rings"]
        with urllib.request.urlopen(f"{base}/overlay/camera/cam-kitchen", timeout=5) as r:
            d = json.loads(r.read())
        assert d["sensor_id"] == "cam-kitchen"
        try:
            urllib.request.urlopen(f"{base}/overlay/camera/bogus", timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            html = r.read().decode()
            assert r.headers["Content-Type"].startswith("text/html")
        assert "Apartment Tracker" in html
        assert "Tomography heat" in html
    finally:
        api.stop()
