"""Dashboard settings: capture reconfiguration, overrides persistence,
system lifecycle endpoints."""

import json
import urllib.error
import urllib.request

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

import hometwin.api as api_mod
from hometwin.api import ApiServer
from hometwin.config import AppConfig, load_config
from hometwin.items import ItemRegistry
from hometwin.sensors.camera import CameraGeometry, CameraSensor, OpenCVFrameSource
from hometwin.tracker import Tracker
from hometwin.world import World, Zone


def write_clip(path, w=64, h=48, n=30):
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (w, h))
    for i in range(n):
        vw.write(np.full((h, w, 3), i * 5, dtype=np.uint8))
    vw.release()


class NullDetector:
    def detect(self, frame):
        return []


def make_tracker(tmp_path):
    clip = tmp_path / "clip.avi"
    write_clip(clip)
    cam = CameraSensor(
        "cam-a",
        geometry=CameraGeometry((0, 0, 2.0), 0.0, 30.0),
        frame_source=OpenCVFrameSource(device=str(clip)),
        detector=NullDetector(),
    )
    cfg = AppConfig(world=World([Zone("r", (0, 0, 0), (5, 5, 3))]),
                    items=ItemRegistry(), sensors=[cam],
                    overrides_path=str(tmp_path / "config.yaml.overrides.json"))
    return Tracker(cfg), cam, tmp_path / "config.yaml.overrides.json"


def post(api, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{api.port}{path}",
        data=json.dumps(body or {}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


def get_json(api, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{api.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_reconfigure_swaps_source_and_persists(tmp_path):
    tracker, cam, overrides = make_tracker(tmp_path)
    cam.frame_source.start()
    old = cam.frame_source
    clip2 = tmp_path / "clip2.avi"
    write_clip(clip2, w=96, h=64)

    desc = tracker.reconfigure_camera("cam-a", {"device": str(clip2)})
    assert cam.frame_source is not old
    assert desc["negotiated"]["width"] == 96
    saved = json.loads(overrides.read_text())
    assert saved["sensors"]["cam-a"]["source"]["device"] == str(clip2)
    cam.frame_source.stop()

    with pytest.raises(KeyError):
        tracker.reconfigure_camera("nope", {})


def test_reconfigure_rolls_back_on_bad_device(tmp_path):
    tracker, cam, overrides = make_tracker(tmp_path)
    cam.frame_source.start()
    with pytest.raises(Exception):
        tracker.reconfigure_camera("cam-a", {"device": str(tmp_path / "missing.avi")})
    # old source restored and still serving
    frame = None
    for _ in range(100):
        frame = cam.frame_source.get_frame()
        if frame is not None:
            break
        import time
        time.sleep(0.02)
    assert frame is not None
    assert not overrides.exists()  # nothing persisted on failure
    cam.frame_source.stop()


def test_config_endpoints(tmp_path):
    tracker, cam, overrides = make_tracker(tmp_path)
    cam.frame_source.start()
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        cams = get_json(api, "/config/cameras")
        assert cams[0]["sensor_id"] == "cam-a"
        assert cams[0]["capture"]["negotiated"]["width"] == 64

        clip2 = tmp_path / "clip2.avi"
        write_clip(clip2, w=96, h=64)
        with post(api, "/config/cameras/cam-a", {"device": str(clip2), "width": "",
                                                 "fps": ""}) as r:
            out = json.loads(r.read())
        assert out["status"] == "applied"
        assert out["capture"]["negotiated"]["width"] == 96
        assert overrides.exists()

        with pytest.raises(urllib.error.HTTPError) as e:
            post(api, "/config/cameras/nope", {})
        assert e.value.code == 404
    finally:
        cam.frame_source.stop()
        api.stop()


def test_overrides_merge_into_loaded_config(tmp_path):
    clip = tmp_path / "clip.avi"
    write_clip(clip)
    (tmp_path / "config.yaml").write_text(f"""
world:
  zones: [{{name: r, min: [0, 0, 0], max: [5, 5, 3]}}]
items: []
sensors:
  - type: camera
    id: cam-a
    position: [0, 0, 2]
    source: {{type: opencv, device: "{clip.as_posix()}"}}
    detector: {{type: aruco, dictionary: DICT_4X4_250}}
""")
    (tmp_path / "config.yaml.overrides.json").write_text(json.dumps(
        {"sensors": {"cam-a": {"source": {"width": 320, "height": 240}}}}))
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.sensors[0].frame_source.width == 320
    assert cfg.sensors[0].frame_source.height == 240
    assert cfg.overrides_path == str(tmp_path / "config.yaml.overrides.json")


def test_system_endpoints(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(api_mod, "_exit_process",
                        lambda tracker, server, relaunch: calls.append(relaunch))
    monkeypatch.delenv("HOMETWIN_REPO", raising=False)
    monkeypatch.delenv("HOMETWIN_CHANNEL", raising=False)

    tracker, cam, _ = make_tracker(tmp_path)
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        with post(api, "/system/restart") as r:
            assert json.loads(r.read())["status"] == "restart"
        with post(api, "/system/shutdown") as r:
            assert json.loads(r.read())["status"] == "shutdown"
        # update without a configured channel is a clean 400, not an exit
        with pytest.raises(urllib.error.HTTPError) as e:
            post(api, "/system/update")
        assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            post(api, "/system/reboot")
        assert e.value.code == 404
    finally:
        api.stop()
    import time
    for _ in range(50):
        if len(calls) == 2:
            break
        time.sleep(0.05)
    assert sorted(calls) == [False, True]  # shutdown: no relaunch; restart: relaunch
