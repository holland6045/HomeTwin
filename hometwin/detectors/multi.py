"""Composite detector: run several detectors on one frame and merge their
detections. Lets a single camera do ArUco tag tracking *and* AI object
detection (RT-DETR person/phone/…) at once — each sub-detector throttles
itself (e.g. rtdetr's interval_s), so the heavy model needn't run every
frame while ArUco does.
"""

from __future__ import annotations

from hometwin.detectors.base import Detector
from hometwin.observations import Detection
from hometwin.registry import create, register


@register("detector", "multi")
class MultiDetector(Detector):
    def __init__(self, detectors: list[dict] | None = None):
        if not detectors:
            raise ValueError("detector 'multi' needs a 'detectors' list")
        self.detectors = []
        for d in detectors:
            cfg = dict(d)
            self.detectors.append(create("detector", cfg.pop("type"), **cfg))

    def detect(self, frame) -> list[Detection]:
        out: list[Detection] = []
        for d in self.detectors:
            out.extend(d.detect(frame))
        return out
