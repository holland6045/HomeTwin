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
import time

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
        out = []
        for det in self.detector.detect(frame):
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
    """USB webcam, RTSP/MJPEG URL (ESP32-CAM), or video file via cv2."""

    def __init__(self, device: int | str = 0):
        self.device = device
        self._cap = None

    def start(self) -> None:
        try:
            import cv2
        except ImportError as e:
            raise RuntimeError(
                "frame_source 'opencv' requires opencv: pip install hometwin[vision]"
            ) from e
        self._cap = cv2.VideoCapture(self.device)
        if not self._cap.isOpened():
            raise RuntimeError(f"cannot open camera {self.device!r}")

    def stop(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def get_frame(self):
        if self._cap is None:
            return None
        ok, frame = self._cap.read()
        return frame if ok else None


@register("frame_source", "static")
class StaticFrameSource:
    """Serves frames from a list — tests and offline replay."""

    def __init__(self, frames: list | None = None):
        self.frames = list(frames or [])

    def get_frame(self):
        return self.frames.pop(0) if self.frames else None
