"""Detector contract: frame in, 2D detections out.

A frame is opaque to the core — it is whatever the paired frame source
produces (numpy array, JPEG bytes, ...). Only the detector needs to
understand it, which keeps cv2/numpy out of the core import graph.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from hometwin.observations import Detection


class Detector(ABC):
    @abstractmethod
    def detect(self, frame) -> list[Detection]: ...
