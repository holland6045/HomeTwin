"""Generic ONNX object detector.

Hardware/model agnostic: any detector exported to ONNX with YOLO-style
output works — pretrained COCO for phones/laptops, or a model fine-tuned on
your own items via the retraining pipeline (training/). Swapping models is a
config change, not a code change.

Requires: pip install hometwin[ml]
"""

from __future__ import annotations

from hometwin.observations import Detection
from hometwin.detectors.base import Detector
from hometwin.registry import register


@register("detector", "onnx")
class OnnxDetector(Detector):
    def __init__(
        self,
        model_path: str,
        labels: list[str] | None = None,
        input_size: int = 640,
        conf_threshold: float = 0.4,
        iou_threshold: float = 0.5,
    ):
        try:
            import numpy as np
            import onnxruntime as ort
        except ImportError as e:
            raise RuntimeError(
                "detector 'onnx' requires onnxruntime+numpy: "
                "pip install hometwin[ml]"
            ) from e
        self._np = np
        self.session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.labels = labels or []
        self.input_size = input_size
        self.conf = conf_threshold
        self.iou = iou_threshold

    def _preprocess(self, frame):
        np = self._np
        import cv2

        img = cv2.resize(frame, (self.input_size, self.input_size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return img.transpose(2, 0, 1)[None]

    def _nms(self, boxes: list[tuple], scores: list[float]) -> list[int]:
        order = sorted(range(len(boxes)), key=lambda i: -scores[i])
        keep: list[int] = []
        for i in order:
            x1, y1, w1, h1 = boxes[i]
            ok = True
            for j in keep:
                x2, y2, w2, h2 = boxes[j]
                ix = max(0.0, min(x1 + w1, x2 + w2) - max(x1, x2))
                iy = max(0.0, min(y1 + h1, y2 + h2) - max(y1, y2))
                inter = ix * iy
                union = w1 * h1 + w2 * h2 - inter
                if union > 0 and inter / union > self.iou:
                    ok = False
                    break
            if ok:
                keep.append(i)
        return keep

    def detect(self, frame) -> list[Detection]:
        np = self._np
        out = self.session.run(None, {self.input_name: self._preprocess(frame)})[0]
        # YOLOv5/v8 layouts: (1, N, 5+C) or (1, 4+C, N)
        pred = out[0]
        if pred.shape[0] < pred.shape[1]:
            pred = pred.T
        boxes, scores, classes = [], [], []
        has_obj = pred.shape[1] >= 5 + max(len(self.labels), 1)
        for row in pred:
            cx, cy, w, h = row[:4]
            cls_scores = row[5:] if has_obj else row[4:]
            obj = float(row[4]) if has_obj else 1.0
            if len(cls_scores) == 0:
                continue
            ci = int(np.argmax(cls_scores))
            score = obj * float(cls_scores[ci])
            if score < self.conf:
                continue
            s = self.input_size
            boxes.append(((cx - w / 2) / s, (cy - h / 2) / s, w / s, h / s))
            scores.append(score)
            classes.append(ci)
        return [
            Detection(
                label=self.labels[classes[i]] if classes[i] < len(self.labels) else str(classes[i]),
                confidence=scores[i],
                bbox=boxes[i],
            )
            for i in self._nms(boxes, scores)
        ]
