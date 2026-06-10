"""Camera sensor: frame source + detector + projection to world space.

Frame sources and detectors are both plugins, so "a camera" can be a USB
webcam, an ESP32-CAM MJPEG stream, or a file of test images — paired with an
ArUco, ONNX, or any custom detector — without this module changing.

Projection model: pinhole at a known pose. A detection's bbox center defines
a ray; we intersect it with a horizontal surface plane (configurable height,
e.g. floor 0.0 or counter 0.9). This gives full 3D from a single camera for
items resting on known surfaces — the dominant case for keys/wallet/phone.
Items not on a configured plane still produce a high-sigma estimate which
fusion refines with other modalities.
"""

from __future__ import annotations

import math
import time

from apartment_tracker.observations import Detection, PositionObservation
from apartment_tracker.registry import create, register
from apartment_tracker.sensors.base import SensorAdapter


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
        wx = fz * cyw - cx * sy
        wy = fz * sy + cx * cyw
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
        cx = -wx * sy + wy * cyw
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
        self.frame_source = frame_source
        self.detector = detector
        self.surface_z = surface_z
        self.base_sigma_m = base_sigma_m

    def start(self) -> None:
        if hasattr(self.frame_source, "start"):
            self.frame_source.start()

    def stop(self) -> None:
        if hasattr(self.frame_source, "stop"):
            self.frame_source.stop()

    def to_observation(self, det: Detection, ts: float) -> PositionObservation | None:
        u, v = det.center
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
        }

    def poll(self) -> list[PositionObservation]:
        frame = self.frame_source.get_frame()
        if frame is None:
            return []
        ts = time.time()
        out = []
        for det in self.detector.detect(frame):
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
                "frame_source 'opencv' requires opencv: pip install apartment-tracker[vision]"
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
