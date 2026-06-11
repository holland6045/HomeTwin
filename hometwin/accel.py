"""Hardware acceleration detection and dispatch.

The core stays pure-Python so it runs anywhere; this module discovers what
the host actually has — CPU count, numpy, OpenCV CUDA devices, ONNX
Runtime execution providers (CUDA/TensorRT/CoreML) — and the heavy paths
consult it: sensor polling sizes its thread pool from it, the ONNX
detector orders its providers by it, tomography picks its vectorized
kernel through it. Report surfaces at GET /health.

Threading model (see docs/performance.md): the GIL is not the bottleneck
here because the hot work happens in C extensions that release it (cv2
decode/detect, numpy kernels, onnxruntime) and in blocking I/O (MJPEG
sockets, V4L). Fusion itself stays single-threaded by design — observation
ordering is what keeps the filters honest.
"""

from __future__ import annotations

import os
from functools import lru_cache


@lru_cache(maxsize=1)
def capabilities() -> dict:
    caps: dict = {"cpu_count": os.cpu_count() or 1}

    try:
        import numpy

        caps["numpy"] = numpy.__version__
    except ImportError:
        caps["numpy"] = None

    caps["cv2"] = None
    caps["cv2_cuda_devices"] = 0
    try:
        import cv2

        caps["cv2"] = cv2.__version__
        try:
            caps["cv2_cuda_devices"] = int(cv2.cuda.getCudaEnabledDeviceCount())
        except Exception:
            pass
    except ImportError:
        pass

    caps["onnx_providers"] = []
    try:
        import onnxruntime as ort

        caps["onnx_providers"] = list(ort.get_available_providers())
    except ImportError:
        pass

    return caps


# preference order for ONNX inference: GPU/NPU first, CPU as the floor
_PROVIDER_RANK = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "CoreMLExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
)


def onnx_providers(available: list[str] | None = None) -> list[str]:
    """Available ONNX providers ordered fastest-first; always ends in CPU."""
    avail = capabilities()["onnx_providers"] if available is None else available
    ranked = [p for p in _PROVIDER_RANK if p in avail]
    if "CPUExecutionProvider" not in ranked:
        ranked.append("CPUExecutionProvider")
    return ranked


def poll_workers(sensor_count: int) -> int:
    """Thread-pool size for parallel sensor polling."""
    return max(1, min(sensor_count, (os.cpu_count() or 2) * 2, 16))
