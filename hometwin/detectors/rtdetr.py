"""RT-DETR / RT-DETRv2 detector plugin.

Why this exists alongside the YOLO-layout `onnx` plugin: the obvious YOLO
weights are AGPL-3.0, while PekingU's RT-DETRv2 is Apache-2.0, NMS-free,
and COCO-trained — person, cell phone, backpack, handbag, laptop are all
day-one classes. Export once on any machine:

    pip install optimum[exporters]
    optimum-cli export onnx --model PekingU/rtdetr_v2_r18vd rtdetr_onnx/

then point config at it:

    detector: {type: rtdetr, model_path: rtdetr_onnx/model.onnx}

Output layout: logits (1, Q, C) + pred_boxes (1, Q, 4) in normalized
cxcywh; focal-style scores (per-class sigmoid, no background softmax), so
decoding is a threshold — no NMS pass. Providers come from the accel
layer (TensorRT/CUDA/CoreML when present).
"""

from __future__ import annotations

from hometwin.detectors.base import Detector
from hometwin.observations import Detection
from hometwin.registry import register

COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]


@register("detector", "rtdetr")
class RTDetrDetector(Detector):
    def __init__(
        self,
        model_path: str | None = None,
        labels: list[str] | None = None,
        input_size: int = 640,
        conf_threshold: float = 0.4,
        max_detections: int = 50,
        # min seconds between inferences; throttled frames return no
        # detections (presence off-delays smooth the gaps). Transformer
        # inference is ~100x an ArUco pass — throttle when on CPU.
        interval_s: float = 0.0,
        session=None,  # injectable for tests
    ):
        try:
            import numpy as np
        except ImportError as e:
            raise RuntimeError(
                "detector 'rtdetr' requires numpy: pip install hometwin[ml]"
            ) from e
        self._np = np
        if session is None:
            try:
                import onnxruntime as ort
            except ImportError as e:
                raise RuntimeError(
                    "detector 'rtdetr' requires onnxruntime: pip install hometwin[ml]"
                ) from e
            from hometwin.accel import onnx_providers

            session = ort.InferenceSession(model_path, providers=onnx_providers())
        self.session = session
        self.input_names = [i.name for i in session.get_inputs()]
        self.labels = labels or COCO_LABELS
        self.input_size = input_size
        self.conf = conf_threshold
        self.max_detections = max_detections
        self.interval_s = interval_s
        self._last_run = float("-inf")

    def _preprocess(self, frame):
        np = self._np
        import cv2

        img = cv2.resize(frame, (self.input_size, self.input_size))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return img.transpose(2, 0, 1)[None]

    def _feeds(self, pixels):
        feeds = {self.input_names[0]: pixels}
        # optimum exports may include orig_target_sizes for the processor
        if len(self.input_names) > 1:
            feeds[self.input_names[1]] = self._np.array(
                [[self.input_size, self.input_size]], dtype=self._np.int64
            )
        return feeds

    @staticmethod
    def _sigmoid(x):
        import numpy as np

        return 1.0 / (1.0 + np.exp(-x))

    def decode(self, logits, boxes) -> list[Detection]:
        """logits (Q, C) + boxes (Q, 4) normalized cxcywh -> Detections.
        Focal-style scoring: per-class sigmoid, threshold, no NMS."""
        np = self._np
        scores = self._sigmoid(np.asarray(logits, dtype=np.float32))
        boxes = np.asarray(boxes, dtype=np.float32)
        best_class = scores.argmax(axis=-1)
        best_score = scores.max(axis=-1)
        order = np.argsort(-best_score)[: self.max_detections]
        out = []
        for idx in order:
            score = float(best_score[idx])
            if score < self.conf:
                break
            cx, cy, w, h = (float(v) for v in boxes[idx])
            ci = int(best_class[idx])
            out.append(
                Detection(
                    label=self.labels[ci] if ci < len(self.labels) else str(ci),
                    confidence=round(score, 4),
                    bbox=(cx - w / 2.0, cy - h / 2.0, w, h),
                )
            )
        return out

    def detect(self, frame) -> list[Detection]:
        if self.interval_s > 0.0:
            import time

            now = time.monotonic()
            if now - self._last_run < self.interval_s:
                return []
            self._last_run = now
        outputs = self.session.run(None, self._feeds(self._preprocess(frame)))
        logits, boxes = outputs[0][0], outputs[1][0]
        # some exports order (boxes, logits): logits have the class dim
        if logits.shape[-1] == 4 and boxes.shape[-1] != 4:
            logits, boxes = boxes, logits
        return self.decode(logits, boxes)
