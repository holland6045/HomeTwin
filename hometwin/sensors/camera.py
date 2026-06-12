"""Camera sensor: frame source + detector + projection to world space.

Frame sources and detectors are both plugins, so "a camera" can be a USB
webcam, an ESP32-CAM MJPEG stream, or a file of test images — paired with an
ArUco, ONNX, or any custom detector — without this module changing.

Two projection modes per camera (config `mode`):

- "surface" (default): intersect the detection ray with a horizontal plane
  (floor 0.0, counter 0.9, ...) -> PositionObservation. Full 3D from one
  camera for items resting on known surfaces.
- "ray": emit the sight ray itself as a BearingObservation. No surface
  assumption; the fusion engine triangulates rays from two or more
  overlapping cameras into true 3D, and falls back to an item-height depth
  prior when only one viewpoint sees the item. Use this for multi-camera
  rooms and for items in motion.
"""

from __future__ import annotations

import math

from hometwin.observations import (
    BearingObservation,
    Detection,
    Observation,
    PositionObservation,
)
from hometwin.registry import create, register
from hometwin.sensors.base import SensorAdapter


class CameraGeometry:
    """Pose + intrinsics; maps normalized pixel coords to world rays."""

    def __init__(
        self,
        position: tuple[float, float, float],
        yaw_deg: float,
        pitch_deg: float,
        hfov_deg: float = 70.0,
        aspect: float = 4.0 / 3.0,
    ):
        self.position = position
        self.yaw = math.radians(yaw_deg)
        self.pitch = math.radians(pitch_deg)
        self.tan_h = math.tan(math.radians(hfov_deg) / 2.0)
        self.tan_v = self.tan_h / aspect

    def ray(self, u: float, v: float) -> tuple[float, float, float]:
        """World-frame unit ray through normalized pixel (u right, v down)."""
        # camera frame: x right, y down, z forward
        cx = (u - 0.5) * 2.0 * self.tan_h
        cy = (v - 0.5) * 2.0 * self.tan_v
        cz = 1.0
        # pitch about camera x (positive = tilt down), then yaw about world z
        sp, cp = math.sin(self.pitch), math.cos(self.pitch)
        fy = cy * cp + cz * sp  # world-down component before yaw
        fz = -cy * sp + cz * cp  # forward component
        sy, cyw = math.sin(self.yaw), math.cos(self.yaw)
        # camera-right maps to (sy, -cy): facing +y means image-right = +x.
        # (The original sign here was mirrored — self-consistent with the
        # inverse below, so simulations passed, but real cameras would have
        # had world x flipped about the optical axis. Caught by PnP.)
        wx = fz * cyw + cx * sy
        wy = fz * sy - cx * cyw
        wz = -fy
        n = math.sqrt(wx * wx + wy * wy + wz * wz)
        return (wx / n, wy / n, wz / n)

    def project_to_plane(
        self, u: float, v: float, plane_z: float
    ) -> tuple[float, float, float] | None:
        dx, dy, dz = self.ray(u, v)
        if abs(dz) < 1e-9:
            return None
        t = (plane_z - self.position[2]) / dz
        if t <= 0:
            return None
        return (self.position[0] + dx * t, self.position[1] + dy * t, plane_z)

    def world_to_pixel(self, p: tuple[float, float, float]) -> tuple[float, float] | None:
        """Inverse of ray(): world point -> normalized pixel, None if behind
        the camera. Coordinates may fall outside [0,1]; callers clip."""
        wx, wy, wz = (p[i] - self.position[i] for i in range(3))
        sy, cyw = math.sin(self.yaw), math.cos(self.yaw)
        fz = wx * cyw + wy * sy
        cx = wx * sy - wy * cyw
        fy = -wz
        sp, cp = math.sin(self.pitch), math.cos(self.pitch)
        cy = fy * cp - fz * sp
        cz = fy * sp + fz * cp
        if cz <= 1e-6:
            return None
        return (cx / cz / (2.0 * self.tan_h) + 0.5, cy / cz / (2.0 * self.tan_v) + 0.5)


@register("sensor", "camera")
class CameraSensor(SensorAdapter):
    def __init__(
        self,
        sensor_id: str,
        geometry: CameraGeometry | None = None,
        frame_source=None,
        detector=None,
        surface_z: float = 0.0,
        base_sigma_m: float = 0.25,
        mode: str = "surface",  # "surface" | "ray"
        bearing_sigma_rad: float = 0.02,
        anchor_correct: bool = False,  # fold anchor residuals back into the pose
        # config-file path: build sub-plugins by name
        position: tuple | None = None,
        yaw_deg: float = 0.0,
        pitch_deg: float = 0.0,
        hfov_deg: float = 70.0,
        source: dict | None = None,
        stream_url: str | None = None,  # browser-loadable MJPEG/snapshot URL
    ):
        super().__init__(sensor_id)
        self.stream_url = stream_url
        self.geometry = geometry or CameraGeometry(
            tuple(position or (0, 0, 2.0)), yaw_deg, pitch_deg, hfov_deg
        )
        if frame_source is None and source:
            frame_source = create("frame_source", source.pop("type"), **source)
        if isinstance(detector, dict):
            cfg = dict(detector)
            detector = create("detector", cfg.pop("type"), **cfg)
        if frame_source is None or detector is None:
            raise ValueError(f"camera {sensor_id!r} needs a frame source and a detector")
        if mode not in ("surface", "ray"):
            raise ValueError(f"camera {sensor_id!r}: mode must be 'surface' or 'ray'")
        self.frame_source = frame_source
        self.detector = detector
        self.surface_z = surface_z
        self.base_sigma_m = base_sigma_m
        self.mode = mode
        self.bearing_sigma_rad = bearing_sigma_rad
        self.anchor_correct = anchor_correct
        self.calibrator = None
        self.movables = None
        self.device_tags = None
        self._soft_refs: dict[str, tuple[tuple, float]] = {}
        # latest capture, kept for /camera/<id>/frame.jpg and diagnostics.
        # Plain reference swaps — readers on other threads get a coherent
        # (frame, ts, detections) at worst one poll stale.
        self.last_frame = None
        self.last_frame_ts = 0.0
        self.last_detections: list[Detection] = []

    def _ensure_calibrator(self):
        if self.calibrator is None:
            from hometwin.anchors import AnchorCalibrator

            self.calibrator = AnchorCalibrator(
                self.geometry, {}, auto_correct=self.anchor_correct
            )
        return self.calibrator

    def attach_movables(self, registry) -> None:
        """Share the tracker-wide movable registry (doors/drawers)."""
        self.movables = registry

    def attach_device_tags(self, solver) -> None:
        """Share the tracker-wide device-tag solver (tags on sensors)."""
        self.device_tags = solver

    def update_soft_references(self, refs: dict[str, tuple[tuple, float]]) -> None:
        """Fused tag positions qualified by the tracker as references:
        {tag: (position, sigma_m)}. Refreshed every step; stale refs vanish."""
        self._soft_refs = refs
        if refs:
            self._ensure_calibrator()

    def attach_anchors(self, anchors: dict[str, tuple[float, float, float]]) -> None:
        """Give this camera the world's calibration anchors (optional)."""
        if not anchors:
            return
        from hometwin.anchors import AnchorCalibrator

        self.calibrator = AnchorCalibrator(
            self.geometry, anchors, auto_correct=self.anchor_correct
        )

    def start(self) -> None:
        if hasattr(self.frame_source, "start"):
            self.frame_source.start()

    def stop(self) -> None:
        if hasattr(self.frame_source, "stop"):
            self.frame_source.stop()

    def to_observation(self, det: Detection, ts: float) -> Observation | None:
        u, v = det.center
        if self.mode == "ray":
            return BearingObservation(
                sensor_id=self.sensor_id,
                timestamp=ts,
                item_id=det.tag_id,
                label=det.label,
                confidence=det.confidence,
                origin=self.geometry.position,
                direction=self.geometry.ray(u, v),
                sigma_rad=self.bearing_sigma_rad,
            )
        pos = self.geometry.project_to_plane(u, v, self.surface_z)
        if pos is None:
            return None
        # uncertainty grows with distance from the camera
        d = math.dist(pos, self.geometry.position)
        return PositionObservation(
            sensor_id=self.sensor_id,
            timestamp=ts,
            item_id=det.tag_id,
            label=det.label,
            confidence=det.confidence,
            position=pos,
            sigma_m=self.base_sigma_m * max(d, 1.0),
        )

    def overlay(self) -> dict:
        return {
            "kind": "camera",
            "sensor_id": self.sensor_id,
            "position": list(self.geometry.position),
            "yaw_deg": math.degrees(self.geometry.yaw),
            "hfov_deg": math.degrees(2.0 * math.atan(self.geometry.tan_h)),
            "surface_z": self.surface_z,
            "stream_url": self.stream_url,
            "calibration": self.calibrator.status() if self.calibrator else None,
        }

    def poll(self) -> list[Observation]:
        frame = self.frame_source.get_frame()
        if frame is None:
            return []
        ts = self.clock()
        dets = self.detector.detect(frame)
        self.last_frame, self.last_frame_ts, self.last_detections = frame, ts, dets
        out = []
        for det in dets:
            if self.movables and det.tag_id and self.movables.observe_ray(
                det.tag_id, self.geometry.position, self.geometry.ray(*det.center), ts
            ):
                # door/drawer tag, not an item — but at a confident mechanical
                # limit its surveyed home doubles as a soft reference
                home = self.movables.closed_reference(det.tag_id)
                if home is not None:
                    self._ensure_calibrator().observe_soft(
                        det.tag_id, *det.center, ts, home, 0.03
                    )
                continue
            if self.device_tags and det.tag_id and self.device_tags.observe_ray(
                det.tag_id, self.geometry.position, self.geometry.ray(*det.center), ts
            ):
                continue  # tag on a sensor device, not a tracked item
            if (
                self.calibrator
                and det.tag_id
                and self.calibrator.observe(det.tag_id, *det.center, ts)
            ):
                continue  # calibration target, not a tracked item
            if det.tag_id and det.tag_id in self._soft_refs:
                pos, sigma_m = self._soft_refs[det.tag_id]
                self._ensure_calibrator().observe_soft(det.tag_id, *det.center, ts, pos, sigma_m)
                # fall through: still a tracked item observation
            obs = self.to_observation(det, ts)
            if obs is not None:
                out.append(obs)
        return out


@register("frame_source", "opencv")
class OpenCVFrameSource:
    """USB webcam, RTSP/MJPEG URL (ESP32-CAM), or video file via cv2.

    Mode negotiation: most UVC webcams default to 640x480 and need the
    MJPG fourcc to deliver their full resolution at full frame rate
    (uncompressed YUY2 saturates USB2 well below 1080p30). Find what the
    hardware supports with `hometwin webcam-probe`, then configure:

        source: {type: opencv, device: 0, width: 1920, height: 1080,
                 fps: 30, fourcc: MJPG}

    A capture thread drains the camera continuously so `get_frame()`
    always returns the freshest frame — without it, cv2 buffers frames
    internally and detection lags seconds behind reality at low poll
    rates. Each frame is handed out once; repeat calls between captures
    return None (no wasted re-detection on identical frames).
    """

    def __init__(
        self,
        device: int | str = 0,
        width: int | None = None,
        height: int | None = None,
        fps: float | None = None,
        fourcc: str | None = None,
        threaded: bool = True,
    ):
        self.device = device
        self.width, self.height, self.fps, self.fourcc = width, height, fps, fourcc
        self.threaded = threaded
        self._cap = None
        self._thread = None
        self._stop = None
        self._lock = None
        self._latest = None  # (seq, frame)
        self._served_seq = 0
        self._negotiated: dict = {}

    def _open(self):
        try:
            import cv2
        except ImportError as e:
            raise RuntimeError(
                "frame_source 'opencv' requires opencv: pip install hometwin[vision]"
            ) from e
        cap = cv2.VideoCapture(self.device)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open camera {self.device!r}")
        # fourcc first: switching to MJPG changes which modes are offered
        if self.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.fourcc))
        if self.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.fps:
            cap.set(cv2.CAP_PROP_FPS, self.fps)
        fcc = int(cap.get(cv2.CAP_PROP_FOURCC)) & 0xFFFFFFFF
        fcc_s = "".join(chr((fcc >> 8 * i) & 0xFF) for i in range(4))
        self._negotiated = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(cap.get(cv2.CAP_PROP_FPS), 1),
            # some backends (MSMF) report a numeric format id, not a fourcc
            "fourcc": fcc_s.strip() if fcc_s.isprintable() else None,
            "backend": cap.getBackendName(),
            "threaded": self.threaded,
        }
        return cap

    def describe(self) -> dict:
        """Requested vs negotiated capture mode, for diagnostics."""
        return {
            "device": self.device,
            "requested": {k: v for k, v in (("width", self.width), ("height", self.height),
                                            ("fps", self.fps), ("fourcc", self.fourcc)) if v},
            "negotiated": dict(self._negotiated),
        }

    def _capture_loop(self) -> None:
        seq = 0
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok:
                self._stop.wait(0.05)  # camera hiccup; keep trying
                continue
            seq += 1
            with self._lock:
                self._latest = (seq, frame)

    def start(self) -> None:
        import threading

        self._cap = self._open()
        if not self.threaded:
            return
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._capture_loop, name=f"capture-{self.device}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._thread.join(timeout=2.0)
            self._thread = self._stop = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def get_frame(self):
        if self._cap is None:
            return None
        if not self.threaded:
            ok, frame = self._cap.read()
            return frame if ok else None
        with self._lock:
            if self._latest is None or self._latest[0] == self._served_seq:
                return None
            self._served_seq, frame = self._latest
        return frame


@register("frame_source", "static")
class StaticFrameSource:
    """Serves frames from a list — tests and offline replay."""

    def __init__(self, frames: list | None = None):
        self.frames = list(frames or [])

    def get_frame(self):
        return self.frames.pop(0) if self.frames else None
