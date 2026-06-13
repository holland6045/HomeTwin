"""Monocular depth: scale fit, the plugin decode, and camera integration
(metric back-projection + dense cloud), all on a fake ONNX session."""

import pytest

np = pytest.importorskip("numpy")

from hometwin.depth import DepthAnything, DepthScale, sample_disparity
from hometwin.observations import Detection
from hometwin.sensors.camera import CameraGeometry, CameraSensor


class FakeDepth:
    """Returns a fixed disparity map regardless of input."""

    def __init__(self, value=2.0, size=70):
        self.map = np.full((size, size), float(value), dtype=np.float32)
        self.name = "pixel_values"

    def get_inputs(self):
        class I:  # noqa: E742
            name = "pixel_values"
        return [I()]

    def run(self, _, feeds):
        return [self.map[None]]


def test_depth_scale_inverse_fit():
    sc = DepthScale()
    assert not sc.ready
    for _ in range(3):
        sc.observe(disparity=2.0, metric_dist=3.0)  # k = 6
    assert sc.ready and sc.k == pytest.approx(6.0, abs=0.5)
    # larger disparity = nearer
    assert sc.metric(4.0) < sc.metric(1.0)
    assert sc.metric(2.0) == pytest.approx(sc.k / 2.0, abs=0.3)


def test_depth_estimator_decode_and_throttle():
    d = DepthAnything(session=FakeDepth(value=3.0), interval_s=60.0)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    m = d.infer(frame)
    assert m is not None and float(m.mean()) == pytest.approx(3.0)
    assert d.infer(frame) is None  # throttled
    assert sample_disparity(m, 0.5, 0.5) == pytest.approx(3.0)


def _camera(**kw):
    geo = CameraGeometry(position=(0.0, 0.0, 2.0), yaw_deg=0, pitch_deg=90)  # straight down

    class Frames:
        def get_frame(self):
            return np.zeros((48, 64, 3), dtype=np.uint8)

    return CameraSensor("cam", geometry=geo, frame_source=Frames(),
                        detector=kw.pop("detector"), surface_z=0.0, **kw)


def test_depth_localizes_off_plane_when_scaled():
    item = Detection(label="mug", confidence=0.9, bbox=(0.45, 0.45, 0.1, 0.1))

    class Det:
        def detect(self, frame):
            return [item]

    # manual scale: disparity 2.0 -> metric k/2 = 1.0 m down the ray
    cam = _camera(detector=Det(), depth_estimator=DepthAnything(session=FakeDepth(value=2.0), interval_s=0.0),
                  depth_scale=2.0)
    obs = cam.poll()[0]
    # straight-down camera at z=2: 1 m along the ray lands at z≈1.0, not the
    # floor (z=0) the plane projection would give
    assert obs.position[2] == pytest.approx(1.0, abs=0.2)

    plane_cam = _camera(detector=Det())  # no depth -> plane projection at z=0
    assert plane_cam.poll()[0].position[2] == pytest.approx(0.0, abs=0.05)


def test_depth_autocalibrates_from_visible_anchor_and_builds_cloud():
    anchor = Detection(label="", confidence=1.0, bbox=(0.0, 0.0, 0.04, 0.04),
                       tag_id="aruco:100")
    item = Detection(label="mug", confidence=0.9, bbox=(0.45, 0.45, 0.1, 0.1))

    class Det:
        def detect(self, frame):
            return [anchor, item]

    cam = _camera(detector=Det(), depth_estimator=DepthAnything(session=FakeDepth(value=2.0), interval_s=0.0),
                  depth_dense_stride=20)
    cam.attach_anchors({"aruco:100": [(0.0, 0.0, 0.0)]})  # 2 m below the camera
    for _ in range(3):  # needs min_samples to trust the scale
        cam.poll()
    assert cam.depth_scale.ready
    # 2 m measured distance at disparity 2.0 -> k≈4
    assert cam.depth_scale.k == pytest.approx(4.0, abs=1.0)
    cloud = cam.drain_cloud()
    assert cloud and all(len(p) == 3 for p in cloud)  # (point, weight, ts)
    assert cam.drain_cloud() == []  # drained once


def test_depth_uncalibrated_falls_back_to_plane():
    item = Detection(label="mug", confidence=0.9, bbox=(0.45, 0.45, 0.1, 0.1))

    class Det:
        def detect(self, frame):
            return [item]

    cam = _camera(detector=Det(),
                  depth_estimator=DepthAnything(session=FakeDepth(value=2.0), interval_s=0.0))
    # no anchor, no manual scale -> not ready -> plane projection (z=0)
    obs = cam.poll()[0]
    assert not cam.depth_scale.ready
    assert obs.position[2] == pytest.approx(0.0, abs=0.05)
    assert cam.drain_cloud() == []


def test_depth_jpg_endpoint(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import json
    import urllib.error
    import urllib.request

    from hometwin.api import ApiServer
    from hometwin.config import AppConfig
    from hometwin.items import ItemRegistry
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    item = Detection(label="mug", confidence=0.9, bbox=(0.45, 0.45, 0.1, 0.1))

    class Det:
        def detect(self, frame):
            return [item]

    cam = _camera(detector=Det(),
                  depth_estimator=DepthAnything(session=FakeDepth(value=2.0), interval_s=0.0),
                  depth_scale=2.0)
    cfg = AppConfig(world=World([Zone("r", (0, 0, 0), (5, 5, 3))]),
                    items=ItemRegistry(), sensors=[cam])
    tr = Tracker(cfg)
    api = ApiServer(tr, "127.0.0.1", 0)
    api.start()
    try:
        # before any keyframe: 404
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{api.port}/camera/cam/depth.jpg", timeout=5)
        assert e.value.code == 404
        tr.step()  # produces a depth map
        with urllib.request.urlopen(f"http://127.0.0.1:{api.port}/camera/cam/depth.jpg",
                                    timeout=5) as r:
            assert r.headers["Content-Type"] == "image/jpeg"
            assert r.read(2)[:2] == b"\xff\xd8"  # JPEG
    finally:
        api.stop()
