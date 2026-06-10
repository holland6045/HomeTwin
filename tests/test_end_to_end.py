"""End-to-end: synthetic apartment, three modalities fused, items located."""

import json
import urllib.request

from apartment_tracker.api import ApiServer
from apartment_tracker.simulate import build_simulation, run_simulation


def test_simulation_locates_all_items():
    result = run_simulation(ticks=120, seed=1)
    errors = result["errors_m"]

    # camera + ArUco: tight fix on the keys, in the right zone
    assert errors["keys"] < 0.3
    assert result["snapshot"]["keys"]["zone"] == "kitchen_counter"

    # BLE trilateration only: coarser, but the right neighbourhood
    assert errors["wallet"] < 1.5
    assert result["snapshot"]["wallet"]["zone"] in ("sofa", "living_room")

    # moving phone still tracked by BLE
    assert errors["phone"] < 2.0

    # tomography saw the person in the living room
    assert result["presence"] is not None
    assert result["presence"]["zone"] == "living_room"


def test_multiple_sensors_contributed():
    result = run_simulation(ticks=60, seed=2)
    wallet = result["snapshot"]["wallet"]
    assert len(wallet["sensors"]) >= 3  # all three BLE scanners fused


def test_api_serves_tracker_state():
    tracker, state = build_simulation(seed=3)
    for _ in range(40):
        state.tick()
        tracker.step()
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        base = f"http://127.0.0.1:{api.port}"
        with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
            assert json.loads(r.read())["status"] == "ok"
        with urllib.request.urlopen(f"{base}/items", timeout=5) as r:
            items = {e["item_id"]: e for e in json.loads(r.read())}
        assert items["keys"]["status"] == "tracked"
        with urllib.request.urlopen(f"{base}/items/House%20keys", timeout=5) as r:
            assert json.loads(r.read())["item_id"] == "keys"
        with urllib.request.urlopen(f"{base}/presence", timeout=5) as r:
            assert json.loads(r.read())["zone"] == "living_room"

        # manual runtime tagging via the API
        req = urllib.request.Request(
            f"{base}/items/keys/tags",
            data=json.dumps({"tag": "ble:CC:CC:CC:CC:CC:CC"}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["status"] == "tagged"
        assert tracker.cfg.items.resolve_tag("ble:CC:CC:CC:CC:CC:CC") == "keys"

        try:
            urllib.request.urlopen(f"{base}/items/jetpack", timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        api.stop()
