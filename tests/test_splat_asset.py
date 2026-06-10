import json
import urllib.error
import urllib.request

from apartment_tracker.api import ApiServer
from apartment_tracker.config import load_config
from apartment_tracker.simulate import build_simulation


def serve(tracker):
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    return api, f"http://127.0.0.1:{api.port}"


def test_splat_asset_served_when_configured(tmp_path):
    blob = b"fake-splat-bytes" * 10
    asset = tmp_path / "apartment.ply"
    asset.write_bytes(blob)
    tracker, _ = build_simulation(seed=1)
    tracker.cfg.splat_asset = str(asset)
    api, base = serve(tracker)
    try:
        with urllib.request.urlopen(f"{base}/assets/splat", timeout=5) as r:
            assert r.read() == blob
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            assert json.loads(r.read())["splat"] is True
    finally:
        api.stop()


def test_splat_asset_404_when_absent():
    tracker, _ = build_simulation(seed=1)
    api, base = serve(tracker)
    try:
        try:
            urllib.request.urlopen(f"{base}/assets/splat", timeout=5)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            assert json.loads(r.read())["splat"] is False
    finally:
        api.stop()


def test_splat_asset_config_parsing(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        "world:\n  splat_asset: scans/apartment.ply\n  zones: []\nitems: []\nsensors: []\n"
    )
    assert load_config(cfg_file).splat_asset == "scans/apartment.ply"
