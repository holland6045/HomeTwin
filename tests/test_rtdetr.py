"""RT-DETRv2 detector plugin: decode, thresholds, person-presence routing."""

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

from hometwin.detectors.rtdetr import COCO_LABELS, RTDetrDetector
from hometwin.observations import Detection


def logit(p):
    return float(np.log(p / (1.0 - p)))


class FakeSession:
    """RT-DETR-shaped ONNX session: (1, Q, C) logits + (1, Q, 4) boxes."""

    def __init__(self, rows):
        q, c = len(rows), len(COCO_LABELS)
        self.logits = np.full((1, q, c), logit(0.01), dtype=np.float32)
        self.boxes = np.zeros((1, q, 4), dtype=np.float32)
        for i, (cls, score, box) in enumerate(rows):
            self.logits[0, i, COCO_LABELS.index(cls)] = logit(score)
            self.boxes[0, i] = box
        self.last_feeds = None

    def get_inputs(self):
        class I:  # noqa: E742
            name = "pixel_values"
        return [I()]

    def run(self, _, feeds):
        self.last_feeds = feeds
        return [self.logits, self.boxes]


def frame():
    return np.zeros((480, 640, 3), dtype=np.uint8)


def test_decode_threshold_and_bbox_convention():
    session = FakeSession([
        ("person", 0.92, (0.5, 0.5, 0.2, 0.6)),       # cxcywh normalized
        ("cell phone", 0.55, (0.25, 0.75, 0.05, 0.1)),
        ("chair", 0.20, (0.1, 0.1, 0.1, 0.1)),        # below threshold
    ])
    det = RTDetrDetector(session=session, conf_threshold=0.4)
    out = det.detect(frame())
    assert [d.label for d in out] == ["person", "cell phone"]
    person = out[0]
    assert isinstance(person, Detection)
    assert person.confidence == pytest.approx(0.92, abs=0.01)
    # cxcywh -> our x,y,w,h (top-left) convention
    assert person.bbox == pytest.approx((0.4, 0.2, 0.2, 0.6), abs=1e-6)
    assert person.center == pytest.approx((0.5, 0.5), abs=1e-6)
    assert session.last_feeds["pixel_values"].shape == (1, 3, 640, 640)


def test_swapped_output_order_handled():
    session = FakeSession([("person", 0.9, (0.5, 0.5, 0.2, 0.4))])

    class Swapped(FakeSession):
        def run(self, _, feeds):
            return [self.boxes, self.logits]
    swapped = Swapped([("person", 0.9, (0.5, 0.5, 0.2, 0.4))])
    a = RTDetrDetector(session=session).detect(frame())
    b = RTDetrDetector(session=swapped).detect(frame())
    assert [d.bbox for d in a] == [d.bbox for d in b]


def test_person_detection_drives_motion_zone_and_presence():
    """Full path: rtdetr person -> camera projection -> presence evidence ->
    synthetic motion sensor ON. Webcam-only PIRs."""
    from hometwin.config import AppConfig
    from hometwin.hass import MotionZone, MotionZoneController
    from hometwin.items import ItemRegistry
    from hometwin.sensors.camera import CameraGeometry, CameraSensor
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    session = FakeSession([("person", 0.9, (0.5, 0.5, 0.3, 0.8))])
    geo = CameraGeometry(position=(2.0, 0.0, 2.2), yaw_deg=90, pitch_deg=50)

    class Frames:
        def get_frame(self):
            return frame()

    cam = CameraSensor("webcam", geometry=geo, frame_source=Frames(),
                       detector=RTDetrDetector(session=session), surface_z=0.0)
    expected = geo.project_to_plane(0.5, 0.5, 0.0)
    zones = MotionZoneController([
        MotionZone("desk-watch", center=expected[:2], radius=0.6, off_delay_s=30),
        MotionZone("elsewhere", center=(20, 20), radius=0.5),
    ])
    cfg = AppConfig(world=World([Zone("room", (0, 0, 0), (8, 8, 3))]),
                    items=ItemRegistry(), sensors=[cam], motion_zones=zones)
    tracker = Tracker(cfg)
    tracker.step()

    snap = {z["name"]: z for z in zones.snapshot()}
    assert snap["desk-watch"]["on"] is True
    assert snap["elsewhere"]["on"] is False
    assert tracker.presence is not None
    assert tracker.presence["zone"] == "room"
    # person never becomes an item track
    assert tracker.engine.tracks == {}


def test_non_presence_labels_unaffected():
    from hometwin.config import AppConfig
    from hometwin.items import ItemRegistry
    from hometwin.sensors.camera import CameraGeometry, CameraSensor
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    session = FakeSession([("chair", 0.9, (0.5, 0.5, 0.3, 0.3))])

    class Frames:
        def get_frame(self):
            return frame()

    cam = CameraSensor("webcam",
                       geometry=CameraGeometry(position=(2, 0, 2.2), yaw_deg=90,
                                               pitch_deg=50),
                       frame_source=Frames(),
                       detector=RTDetrDetector(session=session), surface_z=0.0)
    tracker = Tracker(AppConfig(world=World([Zone("room", (0, 0, 0), (8, 8, 3))]),
                                items=ItemRegistry(), sensors=[cam]))
    tracker.step()
    assert tracker.presence is None  # a chair is not an occupant
