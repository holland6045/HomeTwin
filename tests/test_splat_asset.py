import json
import urllib.error
import urllib.request

from hometwin.api import ApiServer
from hometwin.config import load_config
from hometwin.simulate import build_simulation


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


def test_splat_hot_swap_updates_asset_and_version(tmp_path):
    asset = tmp_path / "apartment.ply"
    asset.write_bytes(b"old-scan")
    tracker, _ = build_simulation(seed=1)
    tracker.cfg.splat_asset = str(asset)
    api, base = serve(tracker)
    try:
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            v1 = json.loads(r.read())["splat_version"]
        assert v1 is not None

        new_scan = b"new-scan-bytes" * 1000
        req = urllib.request.Request(
            f"{base}/assets/splat", data=new_scan, method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            body = json.loads(r.read())
        assert body == {"status": "updated", "bytes": len(new_scan)}
        assert asset.read_bytes() == new_scan
        assert not asset.with_suffix(".ply.tmp").exists()

        with urllib.request.urlopen(f"{base}/assets/splat", timeout=5) as r:
            assert r.read() == new_scan
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            assert json.loads(r.read())["splat_version"] != v1
    finally:
        api.stop()


def test_splat_upload_rejected_without_configured_path():
    tracker, _ = build_simulation(seed=1)
    api, base = serve(tracker)
    try:
        req = urllib.request.Request(
            f"{base}/assets/splat", data=b"scan", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        api.stop()


def test_transform_update_endpoint():
    tracker, _ = build_simulation(seed=1)
    api, base = serve(tracker)
    try:
        req = urllib.request.Request(
            f"{base}/assets/splat/transform",
            data=json.dumps({"scale": 1.8, "rotation_deg": [0, 0, -25]}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["status"] == "updated"
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            t = json.loads(r.read())["splat_transform"]
        assert t == {"scale": 1.8, "rotation_deg": [0, 0, -25]}
    finally:
        api.stop()


def test_update_splat_cli_pushes_to_running_tracker(tmp_path):
    from hometwin.cli import main

    asset = tmp_path / "live.ply"
    asset.write_bytes(b"old")
    new_file = tmp_path / "rebuilt.ply"
    new_file.write_bytes(b"rebuilt-scan" * 100)

    tracker, _ = build_simulation(seed=1)
    tracker.cfg.splat_asset = str(asset)
    api, base = serve(tracker)
    try:
        rc = main([
            "update-splat", str(new_file),
            "--host", "127.0.0.1", "--port", str(api.port),
            "--transform", '{"scale": 2.0}',
        ])
        assert rc == 0
        assert asset.read_bytes() == new_file.read_bytes()
        assert tracker.cfg.splat_transform == {"scale": 2.0}
    finally:
        api.stop()


def test_splat_rebuild_trainer_runs_command(tmp_path):
    from hometwin import registry

    registry.load_plugins()
    out = tmp_path / "splat.ply"
    trainer = registry.create(
        "trainer", "splat_rebuild",
        command="echo scan-from-{capture} > {output}",
        capture_dir="snaps", output_path=str(out),
    )
    result = trainer.train([])
    assert result == {"splat_path": str(out)}
    assert out.read_text().strip() == "scan-from-snaps"


def test_splat_asset_config_parsing(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        "world:\n  splat_asset: scans/apartment.ply\n  zones: []\nitems: []\nsensors: []\n"
    )
    assert load_config(cfg_file).splat_asset == "scans/apartment.ply"
