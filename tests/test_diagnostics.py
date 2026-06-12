"""Latest-frame endpoint and the remote-debug bundle."""

import io
import json
import urllib.request
import zipfile

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from hometwin.config import AppConfig
from hometwin.items import ItemRegistry
from hometwin.observations import Detection
from hometwin.sensors.camera import CameraGeometry, CameraSensor, StaticFrameSource
from hometwin.tracker import Tracker
from hometwin.world import World, Zone


class OneBoxDetector:
    def detect(self, frame):
        return [Detection(label="keys", confidence=0.9, bbox=(0.4, 0.4, 0.2, 0.2),
                          tag_id="aruco:7")]


def make_tracker():
    frame = np.full((48, 64, 3), 40, dtype=np.uint8)
    cam = CameraSensor(
        "cam-test",
        geometry=CameraGeometry((0, 0, 2.0), 0.0, 30.0),
        frame_source=StaticFrameSource([frame.copy(), frame.copy()]),
        detector=OneBoxDetector(),
        surface_z=0.0,
    )
    cfg = AppConfig(world=World([Zone("r", (0, 0, 0), (5, 5, 3))]),
                    items=ItemRegistry(), sensors=[cam])
    return Tracker(cfg), cam


def get(api, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{api.port}{path}", timeout=5)


def test_camera_retains_latest_frame():
    tracker, cam = make_tracker()
    assert cam.last_frame is None
    tracker.step()
    assert cam.last_frame is not None
    assert cam.last_frame_ts > 0
    assert [d.tag_id for d in cam.last_detections] == ["aruco:7"]


def test_frame_endpoint_serves_jpeg():
    from hometwin.api import ApiServer

    tracker, _ = make_tracker()
    tracker.step()
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        with get(api, "/camera/cam-test/frame.jpg?annotate=1") as r:
            assert r.headers["Content-Type"] == "image/jpeg"
            body = r.read()
        img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        assert img.shape == (48, 64, 3)
        with pytest.raises(urllib.error.HTTPError) as e:
            get(api, "/camera/nope/frame.jpg")
        assert e.value.code == 404
    finally:
        api.stop()


def test_frame_endpoint_404_before_first_frame():
    from hometwin.api import ApiServer

    tracker, _ = make_tracker()  # no step(): nothing captured yet
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            get(api, "/camera/cam-test/frame.jpg")
        assert e.value.code == 404
    finally:
        api.stop()


def test_debug_bundle_contents():
    from hometwin.api import ApiServer

    tracker, _ = make_tracker()
    tracker.step()
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        with get(api, "/debug/bundle") as r:
            assert r.headers["Content-Type"] == "application/zip"
            assert "hometwin-diag-" in r.headers["Content-Disposition"]
            body = r.read()
    finally:
        api.stop()
    z = zipfile.ZipFile(io.BytesIO(body))
    names = set(z.namelist())
    assert {"health.json", "overlay_map.json", "items.json", "events.json",
            "camera_cam-test.json", "camera_cam-test.jpg"} <= names
    health = json.loads(z.read("health.json"))
    assert health["sensors"][0]["id"] == "cam-test"
    cam_state = json.loads(z.read("camera_cam-test.json"))
    assert cam_state["has_frame"] is True
    assert cam_state["detections"][0]["tag_id"] == "aruco:7"
    assert cam_state["overlay"]["sensor_id"] == "cam-test"
