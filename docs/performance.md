# Performance: threading and acceleration

Design rule: concurrency where the hardware wins, single-threaded where
correctness wins.

## Threading model

| Stage | Concurrency | Why |
|---|---|---|
| Sensor polling | Thread pool (`accel.poll_workers`: up to 2x cores, cap 16) | Polling is blocking I/O (MJPEG sockets, V4L, serial) and C-extension work (cv2 decode/detect, onnxruntime) — all of which release the GIL, so cameras stop serializing behind each other. One sensor failing or stalling (10 s timeout) never starves the rest. |
| Fusion | Single tracker thread | Kalman updates are order-sensitive; observation ordering is what keeps the filters honest. Per-observation cost is microseconds — fusion has never been the bottleneck. |
| MCU bridge | Thread per connection | Pure I/O; 64 KiB line cap bounds memory. |
| HTTP API | Thread per request | Reads consume locked copies (`Tracker.overlay_state`), so dashboards never contend with fusion beyond a brief lock. |
| Dashboard rendering | The viewing browser | Canvas/WebGL work never costs the tracker host anything. |

`tracker.parallel_polling: false` forces the old serial loop (debugging).

## GPU / CUDA

- **ONNX detectors** automatically order execution providers
  TensorRT → CUDA → CoreML → DirectML → CPU based on what the installed
  onnxruntime offers (`pip install onnxruntime-gpu` on a CUDA host and
  detection moves to the GPU with zero config).
- **Capability report**: `GET /health` → `accel` shows cores, numpy,
  OpenCV CUDA device count, and ONNX providers — first thing to check
  when sizing new hardware.
- Splat training/rendering stays off-host by design (GPU PC + browser).

## Algorithmic hot paths

- **RF tomography** reconstruction precomputes each link's ellipse cell
  mask once (geometry is static) — per-poll work drops from
  O(links x cells) trig to indexed adds, vectorized via `np.add.at`
  when numpy is installed, pure-Python otherwise.
- Fusion matrices are <= 6x6; pure Python beats numpy's call overhead at
  that size, measured before choosing.
