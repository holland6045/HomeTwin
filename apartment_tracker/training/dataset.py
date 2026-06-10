"""Dataset capture for retraining.

The tracker can record what its sensors see alongside ground-truth labels
the user supplies ("that detection was my wallet", "tag ble:XX was 2.1 m
from scanner hall"). Records are JSONL — trivially consumed by any training
stack, on this machine or elsewhere.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class DatasetRecorder:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _append(self, name: str, record: dict) -> None:
        record.setdefault("timestamp", time.time())
        with open(self.root / f"{name}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def record_detection(
        self, label: str, bbox: tuple, image_ref: str, sensor_id: str, correct: bool = True
    ) -> None:
        self._append(
            "detections",
            {
                "label": label,
                "bbox": list(bbox),
                "image": image_ref,
                "sensor_id": sensor_id,
                "correct": correct,
            },
        )

    def record_rssi_sample(self, sensor_id: str, mac: str, rssi: float, true_dist_m: float) -> None:
        self._append(
            "rssi",
            {"sensor_id": sensor_id, "mac": mac, "rssi": rssi, "dist_m": true_dist_m},
        )

    def load(self, name: str) -> list[dict]:
        path = self.root / f"{name}.jsonl"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
