"""ArUco fiducial detector — the cheapest reliable visual tag.

Print a marker, stick it on the item, map "aruco:<id>" to the item in
config. Requires opencv-contrib (pip install hometwin[vision]).
"""

from __future__ import annotations

from hometwin.observations import Detection
from hometwin.detectors.base import Detector
from hometwin.registry import register


@register("detector", "aruco")
class ArucoDetector(Detector):
    def __init__(self, dictionary: str = "DICT_4X4_250"):
        try:
            import cv2
            import cv2.aruco  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "detector 'aruco' requires opencv-contrib: "
                "pip install hometwin[vision]"
            ) from e
        self._cv2 = cv2
        self._detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary)),
            cv2.aruco.DetectorParameters(),
        )

    def detect(self, frame) -> list[Detection]:
        h, w = frame.shape[:2]
        corners, ids, _ = self._detector.detectMarkers(frame)
        if ids is None:
            return []
        out = []
        for quad, marker_id in zip(corners, ids.flatten()):
            xs = [p[0] / w for p in quad[0]]
            ys = [p[1] / h for p in quad[0]]
            out.append(
                Detection(
                    label="aruco",
                    confidence=1.0,
                    bbox=(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)),
                    tag_id=f"aruco:{int(marker_id)}",
                )
            )
        return out
