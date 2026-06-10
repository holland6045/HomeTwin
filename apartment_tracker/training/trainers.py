"""Trainer plugins: retrain parts of the system from recorded datasets.

Trainer contract: train(records) -> dict of artifacts/parameters. What a
"model" is differs per modality — path-loss constants for BLE, an ONNX file
for vision — so artifacts are an open dict the caller applies to config.

`detector_finetune` deliberately shells out to a user-supplied command
(ultralytics, a cloud job, anything): the heavy training stack is not a
dependency of the tracker, and the resulting ONNX file slots into the
existing `onnx` detector via config.
"""

from __future__ import annotations

import math
import subprocess

from apartment_tracker.registry import register


@register("trainer", "path_loss")
class PathLossTrainer:
    """Closed-form fit of tx_power and exponent from (rssi, dist) samples.

    rssi = tx - 10*n*log10(d): linear regression of rssi on log10(d).
    """

    def train(self, records: list[dict]) -> dict:
        samples = [
            (math.log10(r["dist_m"]), float(r["rssi"]))
            for r in records
            if r.get("dist_m", 0) > 0
        ]
        if len(samples) < 2:
            raise ValueError("need at least 2 rssi samples with positive distance")
        n = len(samples)
        mx = sum(x for x, _ in samples) / n
        my = sum(y for _, y in samples) / n
        sxx = sum((x - mx) ** 2 for x, _ in samples)
        if sxx < 1e-9:
            raise ValueError("rssi samples need at least 2 distinct distances")
        sxy = sum((x - mx) * (y - my) for x, y in samples)
        slope = sxy / sxx  # = -10n
        intercept = my - slope * mx  # = tx_power
        return {"tx_power": intercept, "exponent": -slope / 10.0}


@register("trainer", "splat_rebuild")
class SplatRebuildTrainer:
    """Run an external splat pipeline (nerfstudio/OpenSplat/colmap) on a
    capture directory; same shell-out pattern as detector_finetune. The GPU
    stack is never a dependency of the tracker.

      command: "ns-process-data images --data {capture} --output-dir {work}
                && ns-train splatfacto --data {work} ... && cp ... {output}"
    """

    def __init__(self, command: str, capture_dir: str, output_path: str, work_dir: str = "work"):
        self.command = command
        self.capture_dir = capture_dir
        self.output_path = output_path
        self.work_dir = work_dir

    def train(self, records: list[dict]) -> dict:
        cmd = self.command.format(
            capture=self.capture_dir, output=self.output_path, work=self.work_dir
        )
        subprocess.run(cmd, shell=True, check=True)
        return {"splat_path": self.output_path}


@register("trainer", "detector_finetune")
class DetectorFinetuneTrainer:
    """Run an external training command against the recorded detection set.

    The command receives the dataset dir and output path via format fields:
      command: "yolo detect train data={dataset} ... && cp best.onnx {output}"
    """

    def __init__(self, command: str, dataset_dir: str, output_path: str):
        self.command = command
        self.dataset_dir = dataset_dir
        self.output_path = output_path

    def train(self, records: list[dict]) -> dict:
        cmd = self.command.format(dataset=self.dataset_dir, output=self.output_path)
        subprocess.run(cmd, shell=True, check=True)
        return {"model_path": self.output_path}
