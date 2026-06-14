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
        depth: dict | None = None,      # optional monocular-depth block
        depth_estimator=None,           # injectable (tests)
        depth_scale: float | None = None,  # manual relative->metric override
        depth_dense: bool = True,       # deposit dense world-model points
        depth_dense_stride: int = 24,   # pixel stride for the dense cloud
    ):
        super().__init__(sensor_id)
        self.stream_url = stream_url
        self.geometry = geometry or CameraGeometry(
            tuple(position or (0, 0, 2.0)), yaw_deg, pitch_deg, hfov_deg
        )
        if frame_source is None and source:
            frame_source = create("frame_source", source.pop("type"), **source)
        self.detector_cfg = dict(detector) if isinstance(detector, dict) else None
        if isinstance(detector, dict):
            cfg = dict(detector)
            detector = create("detector", cfg.pop("type"), **cfg)
        if frame_source is None or detector is None:
            raise ValueError(f"camera {sensor_id!r} needs a frame source and a detector")
        if mode not in ("surface", "ray"):
            raise ValueError(f"camera {sensor_id!r}: mode must be 'surface' or 'ray'")
        if depth_estimator is None and depth:
            cfg = dict(depth)
            depth_scale = cfg.pop("scale", depth_scale)
            depth_dense = cfg.pop("dense", depth_dense)
            depth_dense_stride = cfg.pop("dense_stride", depth_dense_stride)
            depth_estimator = create("depth", cfg.pop("type"), **cfg)
        self.depth = depth_estimator
        self.depth_dense = depth_dense
        self.depth_dense_stride = depth_dense_stride
        self._depth_map = None
        self._cloud: list = []
        from hometwin.depth import DepthScale

        self.depth_scale = DepthScale(k=depth_scale)
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
        # occupant classes localize from their feet (bbox bottom) onto the
        # floor, not the item surface; attached from config.presence_labels
        self._presence_labels: frozenset = frozenset()
        self.floor_z = 0.0
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

    def attach_presence_labels(self, labels) -> None:
        """Detector classes to treat as occupants (feet-on-floor projection)."""
        self._presence_labels = frozenset(labels or ())

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

    def probe_modes(self) -> list[dict]:
        """Capture modes this camera's device supports. Only meaningful for a
        local webcam (integer device); releases and reacquires it, so the
        live feed blinks briefly."""
        device = getattr(self.frame_source, "device", None)
        if not isinstance(device, int):
            return []
        running = (getattr(self.frame_source, "_thread", None) is not None
                   or getattr(self.frame_source, "_cap", None) is not None)
        if running:
            self.frame_source.stop()
        try:
            return probe_camera_modes(device)
        finally:
            if running:
                self.frame_source.start()  # self-heals if the device is still settling

    def to_observation(
        self, det: Detection, ts: float, metric_dist: float | None = None
    ) -> Observation | None:
        # occupants are localized from their feet (bbox bottom-center) on
        # the floor; items from their centroid on the configured surface
        presence = not det.tag_id and det.label in self._presence_labels
        if presence:
            x, y, w, h = det.bbox
            u, v = x + w / 2.0, y + h
            plane_z, sigma_scale = self.floor_z, 2.0
        else:
            u, v = det.center
            plane_z, sigma_scale = self.surface_z, 1.0
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
        if metric_dist is not None:
            # back-project the sight ray to the depth-derived distance: true
            # 3D for items off the surface plane, no plane assumption
            ray = self.geometry.ray(u, v)
            pos = tuple(self.geometry.position[i] + ray[i] * metric_dist for i in range(3))
            sigma = max(0.1, 0.06 * metric_dist) * sigma_scale
        else:
            pos = self.geometry.project_to_plane(u, v, plane_z)
            if pos is None:
                return None
            # uncertainty grows with distance from the camera
            sigma = self.base_sigma_m * max(math.dist(pos, self.geometry.position), 1.0) * sigma_scale
        return PositionObservation(
            sensor_id=self.sensor_id,
            timestamp=ts,
            item_id=det.tag_id,
            label=det.label,
            confidence=det.confidence,
            position=pos,
            sigma_m=sigma,
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
            "depth": self.depth_scale.status() if self.depth is not None else None,
        }

    def poll(self) -> list[Observation]:
        frame = self.frame_source.get_frame()
        if frame is None:
            return []
        ts = self.clock()
        dets = self.detector.detect(frame)
        self.last_frame, self.last_frame_ts, self.last_detections = frame, ts, dets
        if self.depth is not None:
            depth_map = self.depth.infer(frame)
            if depth_map is not None:
                self._depth_map = depth_map
                self._calibrate_depth(dets)
                self._build_cloud(ts)
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
            obs = self.to_observation(det, ts, metric_dist=self._depth_dist(det))
            if obs is not None:
                out.append(obs)
        return out

    def _depth_dist(self, det: Detection) -> float | None:
        """Metric distance for an item detection from the depth map, or None
        (uncalibrated / no depth / occupant on the floor path)."""
        if (self._depth_map is None or not self.depth_scale.ready
                or (not det.tag_id and det.label in self._presence_labels)):
            return None
        from hometwin.depth import sample_disparity

        return self.depth_scale.metric(sample_disparity(self._depth_map, *det.center))

    def _calibrate_depth(self, dets) -> None:
        """Any detected tag whose world position is known (a surveyed anchor)
        gives one labeled relative->metric sample: disparity at its pixel vs
        its true distance from the camera."""
        anchors = getattr(self.calibrator, "anchors", None) if self.calibrator else None
        if not anchors:
            return
        from hometwin.depth import sample_disparity

        cam = self.geometry.position
        for det in dets:
            positions = anchors.get(det.tag_id) if det.tag_id else None
            if not positions:
                continue
            dist = min(math.dist(cam, p) for p in positions)
            self.depth_scale.observe(sample_disparity(self._depth_map, *det.center), dist)

    def _build_cloud(self, ts: float) -> None:
        """Back-project a strided grid of the depth map into world points for
        the passive world model — a dense sketch from one camera."""
        if not (self.depth_dense and self.depth_scale.ready):
            return
        from hometwin.depth import sample_disparity

        cam, step = self.geometry.position, self.depth_dense_stride
        h, w = self._depth_map.shape[:2]
        pts = []
        for py in range(step // 2, h, step):
            for px in range(step // 2, w, step):
                metric = self.depth_scale.metric(float(self._depth_map[py, px]))
                if metric is None or metric > 12.0:
                    continue
                ray = self.geometry.ray((px + 0.5) / w, (py + 0.5) / h)
                pts.append((tuple(cam[i] + ray[i] * metric for i in range(3)), 0.2, ts))
        self._cloud = pts

    def drain_cloud(self) -> list:
        """Hand the latest dense point batch to the tracker, once."""
        cloud, self._cloud = self._cloud, []
        return cloud


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
        seq, fails = 0, 0
        while not self._stop.is_set():
            if self._cap is None:  # (re)acquire: device lost, or a deferred open
                try:
                    self._cap = self._open()
                except Exception:
                    self._stop.wait(0.3)
                    continue
            try:
                ok, frame = self._cap.read()
            except Exception:
                ok, frame = False, None
            if not ok:
                fails += 1
                if fails >= 30:  # ~1.5 s of failed reads: drop the wedged handle
                    try:                 # so the next loop reacquires it. This is
                        self._cap.release()  # the Windows MSMF reacquire-after-
                    except Exception:        # release recovery path.
                        pass
                    self._cap, fails = None, 0
                self._stop.wait(0.05)
                continue
            fails, seq = 0, seq + 1
            with self._lock:
                self._latest = (seq, frame)

    def start(self) -> None:
        import threading

        if not self.threaded:
            self._cap = self._open()
            return
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # open synchronously so describe()/reconfigure see the negotiated mode,
        # but tolerate failure — the capture thread keeps retrying to acquire
        try:
            self._cap = self._open()
        except Exception:
            self._cap = None
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


def probe_camera_modes(device, frames: int = 12) -> list[dict]:
    """Enumerate the capture modes a webcam actually delivers: walk a
    resolution ladder with and without MJPG, measure real fps, and report
    distinct {width, height, fps, fourcc, aspect} the hardware honored.
    Opens/closes the device — don't call while it's in active use."""
    import time as _t

    import cv2

    ladder = [(640, 480), (1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]
    out: list[dict] = []
    for fourcc in ("MJPG", None):
        for w, h in ladder:
            cap = cv2.VideoCapture(device)
            if not cap.isOpened():
                cap.release()
                return out
            if fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, 60)
            ok, frame = cap.read()
            if not ok:
                cap.release()
                continue
            ah, aw = frame.shape[:2]
            for _ in range(3):  # let exposure settle before timing
                cap.read()
            n, t0 = 0, _t.time()
            while n < frames and _t.time() - t0 < 3.0:
                if cap.read()[0]:
                    n += 1
            cap.release()
            _t.sleep(0.2)  # let the driver fully release before the next open
            label = fourcc or "default"
            mode = {"width": aw, "height": ah, "fps": round(n / max(_t.time() - t0, 1e-6), 1),
                    "fourcc": label, "aspect": round(aw / ah, 3)}
            if not any(m["width"] == aw and m["height"] == ah and m["fourcc"] == label
                       for m in out):
                out.append(mode)
    return out


@register("frame_source", "static")
class StaticFrameSource:
    """Serves frames from a list — tests and offline replay."""

    def __init__(self, frames: list | None = None):
        self.frames = list(frames or [])

    def get_frame(self):
        return self.frames.pop(0) if self.frames else None
