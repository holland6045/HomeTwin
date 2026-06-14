"""Composite detector, runtime detector swap / AI enablement, the in-app
tag maker, and camera mode probing."""

import json
import urllib.error
import urllib.request

import pytest

from hometwin.observations import Detection
from hometwin.registry import create, load_plugins, register


def test_multi_detector_merges_sub_detectors():
    load_plugins()

    @register("detector", "_stub_a")
    class A:
        def detect(self, frame):
            return [Detection(label="a", confidence=1.0, bbox=(0, 0, 1, 1))]

    @register("detector", "_stub_b")
    class B:
        def detect(self, frame):
            return [Detection(label="b", confidence=1.0, bbox=(0, 0, 1, 1)),
                    Detection(label="c", confidence=1.0, bbox=(0, 0, 1, 1))]

    m = create("detector", "multi", detectors=[{"type": "_stub_a"}, {"type": "_stub_b"}])
    assert [d.label for d in m.detect(None)] == ["a", "b", "c"]
    with pytest.raises(ValueError):
        create("detector", "multi", detectors=[])


def _tracker_one_camera(tmp_path):
    pytest.importorskip("numpy")
    from hometwin.config import AppConfig
    from hometwin.items import ItemRegistry
    from hometwin.sensors.camera import CameraGeometry, CameraSensor, StaticFrameSource
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    cam = CameraSensor("cam", geometry=CameraGeometry((0, 0, 2), 0, 90),
                       frame_source=StaticFrameSource([]), detector=object())
    cam.detector_cfg = {"type": "aruco", "dictionary": "DICT_4X4_250"}
    cfg = AppConfig(world=World([Zone("r", (0, 0, 0), (5, 5, 3))]),
                    items=ItemRegistry(), sensors=[cam],
                    overrides_path=str(tmp_path / "config.yaml.overrides.json"))
    return Tracker(cfg), cam


def test_enable_ai_composes_multi_with_existing_detector(tmp_path, monkeypatch):
    from hometwin.tracker import Tracker

    tr, cam = _tracker_one_camera(tmp_path)
    captured = {}
    monkeypatch.setattr(Tracker, "reconfigure_detector",
                        lambda self, sid, cfg: captured.update(sid=sid, cfg=cfg))
    cfg = tr.enable_ai_detection("cam", "models/rtdetr.onnx")
    assert cfg["type"] == "multi"
    types = [d["type"] for d in cfg["detectors"]]
    assert "aruco" in types and "rtdetr" in types
    assert captured["cfg"] == cfg
    with pytest.raises(KeyError):
        tr.enable_ai_detection("nope", "models/rtdetr.onnx")


def test_reconfigure_detector_swaps_and_persists(tmp_path):
    load_plugins()

    @register("detector", "_stub_swap")
    class S:
        def __init__(self, **kw):
            self.kw = kw

        def detect(self, frame):
            return []

    tr, cam = _tracker_one_camera(tmp_path)
    tr.reconfigure_detector("cam", {"type": "_stub_swap", "foo": 1})
    assert isinstance(cam.detector, S) and cam.detector.kw == {"foo": 1}
    saved = json.loads((tmp_path / "config.yaml.overrides.json").read_text())
    assert saved["sensors"]["cam"]["detector"]["type"] == "_stub_swap"


def _serve(tr):
    from hometwin.api import ApiServer

    api = ApiServer(tr, "127.0.0.1", 0)
    api.start()
    return api


def test_make_tag_endpoint(tmp_path):
    pytest.importorskip("cv2")
    tr, _ = _tracker_one_camera(tmp_path)
    api = _serve(tr)
    try:
        url = f"http://127.0.0.1:{api.port}/make-tag?id=7&ident=KEY/01&caption=keys"
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.headers["Content-Type"] == "image/svg+xml"
            body = r.read().decode()
        assert "<svg" in body and "KEY/01" in body
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{api.port}/make-tag?id=notanumber",
                                   timeout=5)
        assert e.value.code == 400
    finally:
        api.stop()


def test_camera_modes_endpoint_non_webcam_returns_empty(tmp_path):
    # StaticFrameSource has no integer device -> probe yields nothing, no crash
    tr, _ = _tracker_one_camera(tmp_path)
    api = _serve(tr)
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{api.port}/config/cameras/cam/modes", timeout=5) as r:
            assert json.loads(r.read())["modes"] == []
    finally:
        api.stop()
