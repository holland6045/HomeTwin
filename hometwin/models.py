"""Model zoo: pre-exported ONNX models fetched on demand, no torch/optimum
toolchain. Shared by the `get-model` CLI and the dashboard's one-click
detector enablement.
"""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

MODEL_ZOO = {
    # RT-DETR r18vd (Apache-2.0, PekingU): COCO classes; layout matches the
    # rtdetr detector plugin. Variants trade accuracy for size/speed.
    "rtdetr": {
        "repo": "onnx-community/rtdetr_r18vd",
        "files": {
            "fp32": ("onnx/model.onnx",
                     "11843b02455cc24009aed24d4c40db721b1093be5ccd6bbe7b9c441abb1d0558"),
            "fp16": ("onnx/model_fp16.onnx", None),
            "int8": ("onnx/model_int8.onnx", None),
            "quantized": ("onnx/model_quantized.onnx", None),
        },
    },
    # Depth Anything V2 small (Apache-2.0): relative inverse-depth, scaled to
    # metric against a known fiducial for monocular 3D + a dense cloud.
    "depth": {
        "repo": "onnx-community/depth-anything-v2-small",
        "files": {
            "fp32": ("onnx/model.onnx",
                     "afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c"),
            "fp16": ("onnx/model_fp16.onnx", None),
            "int8": ("onnx/model_int8.onnx", None),
            "quantized": ("onnx/model_quantized.onnx", None),
        },
    },
}


def model_path(name: str) -> Path:
    return Path(f"models/{name}.onnx")


def download_model(name: str, variant: str = "fp32", output: str | Path | None = None,
                   progress=None) -> Path:
    """Fetch a zoo model to `output` (default models/<name>.onnx), verifying
    the pinned checksum where one exists. Idempotent: an existing file with
    the right checksum is reused. `progress(bytes_done)` is optional."""
    entry = MODEL_ZOO.get(name)
    if entry is None:
        raise KeyError(f"unknown model {name!r}; available: {', '.join(MODEL_ZOO)}")
    if variant not in entry["files"]:
        raise KeyError(f"unknown variant {variant!r}; available: {', '.join(entry['files'])}")
    remote, sha = entry["files"][variant]
    out = Path(output) if output else model_path(name)
    if out.exists() and sha and _sha256(out) == sha:
        return out  # already have a verified copy
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    url = f"https://huggingface.co/{entry['repo']}/resolve/main/{remote}"
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
    if sha and digest.hexdigest() != sha:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"checksum mismatch for {name} (got {digest.hexdigest()})")
    tmp.replace(out)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
