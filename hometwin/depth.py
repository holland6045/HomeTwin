"""Monocular depth: a single camera that sees a known-distance fiducial can
turn a relative depth map into metric 3D for everything else in frame.

Depth Anything V2 (small, Apache-2.0) outputs relative *inverse* depth
(disparity): larger = nearer. With one reference of known metric distance
the whole map scales — `metric ~= scale / disparity`. That replaces the
surface-plane and item-height-prior assumptions for off-surface items and
deposits a dense point cloud into the world model (a Waymo-style sketch
from one webcam). Export-free: `hometwin get-model depth`.

The estimator is a plugin (kind "depth"); the camera builds it from a
`depth:` config block and owns the scale fit. Inference is throttled to a
keyframe rate — a transformer pass is far heavier than an ArUco decode.
"""

from __future__ import annotations

import time

from hometwin.registry import register


class DepthScale:
    """Online relative->metric scale: metric = k / disparity. Each labeled
    sample (a fiducial of known distance, disparity read at its pixel)
    refines k by EMA; bounded readiness so an uncalibrated camera stays on
    its geometric fallback rather than emitting garbage depth."""

    def __init__(self, k: float | None = None, min_samples: int = 3, alpha: float = 0.2):
        self.k = k
        self.n = 0 if k is None else min_samples
        self.min_samples = min_samples
        self.alpha = alpha

    def observe(self, disparity: float, metric_dist: float) -> None:
        if disparity <= 0.0 or metric_dist <= 0.0:
            return
        sample = disparity * metric_dist
        self.k = sample if self.k is None else (1 - self.alpha) * self.k + self.alpha * sample
        self.n += 1

    @property
    def ready(self) -> bool:
        return self.k is not None and self.n >= self.min_samples

    def metric(self, disparity: float) -> float | None:
        if not self.ready or disparity <= 1e-6:
            return None
        return self.k / disparity

    def status(self) -> dict:
        return {"ready": self.ready, "k": round(self.k, 4) if self.k else None,
                "samples": self.n}


@register("depth", "depth_anything")
class DepthAnything:
    """ONNX Depth Anything V2 wrapper. `infer(frame)` returns a relative
    inverse-depth map (H,W float32) at model resolution, or None when the
    keyframe interval hasn't elapsed. Session is injectable for tests."""

    # ImageNet normalization; input dims must be multiples of 14
    MEAN = (0.485, 0.456, 0.406)
    STD = (0.229, 0.224, 0.225)

    def __init__(self, model_path: str | None = None, input_size: int = 518,
                 interval_s: float = 1.0, session=None):
        try:
            import numpy as np
        except ImportError as e:
            raise RuntimeError("depth 'depth_anything' requires numpy: "
                               "pip install hometwin[ml]") from e
        self._np = np
        if session is None:
            try:
                import onnxruntime as ort
            except ImportError as e:
                raise RuntimeError("depth 'depth_anything' requires onnxruntime: "
                                   "pip install hometwin[ml]") from e
            from hometwin.accel import onnx_providers

            session = ort.InferenceSession(model_path, providers=onnx_providers())
        self.session = session
        self.input_name = session.get_inputs()[0].name
        self.input_size = input_size - (input_size % 14)
        self.interval_s = interval_s
        self._last_run = float("-inf")

    def _preprocess(self, frame):
        np = self._np
        import cv2

        n = self.input_size
        img = cv2.resize(frame, (n, n))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        img = (img - np.array(self.MEAN, np.float32)) / np.array(self.STD, np.float32)
        return img.transpose(2, 0, 1)[None]

    def infer(self, frame):
        if self.interval_s > 0.0:
            now = time.monotonic()
            if now - self._last_run < self.interval_s:
                return None
            self._last_run = now
        out = self.session.run(None, {self.input_name: self._preprocess(frame)})[0]
        return self._np.asarray(out[0], dtype=self._np.float32)


def sample_disparity(depth_map, u: float, v: float) -> float:
    """Nearest-pixel disparity at normalized image coords (u,v in 0..1)."""
    h, w = depth_map.shape[:2]
    x = min(max(int(round(u * (w - 1))), 0), w - 1)
    y = min(max(int(round(v * (h - 1))), 0), h - 1)
    return float(depth_map[y, x])
