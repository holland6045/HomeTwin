# HomeTwin on a Linux server — the central coordinator

Target deployment: a headless Linux NUC running the fusion coordinator
full-time, with an OcuLink-attached NVIDIA GPU (4060 Ti) for ONNX
inference (RT-DETRv2 person detection, future depth/zero-shot models).
Cameras and MCU nodes feed it over the network; the Windows/macOS apps
remain handy as portable test rigs.

## Install

```bash
git clone https://github.com/holland6045/HomeTwin.git && cd HomeTwin
HOMETWIN_CHANNEL=claude/apartment-tracking-cv-app-9n823r \
HOMETWIN_GPU=1 \
packaging/linux/install.sh
```

The script creates a venv under `~/.local/share/hometwin`, writes a
starting config to `~/.config/hometwin/config.yaml`, installs a systemd
**user** service, enables lingering (survives logout/reboot — it's a
server), and starts it. Re-running the script updates in place.

`HOMETWIN_GPU=1` installs the `ml-cuda` extra (`onnxruntime-gpu`)
instead of CPU-only `onnxruntime`.

## Service lifecycle

| What | How |
|---|---|
| Status / logs | `systemctl --user status hometwin` / `journalctl --user -u hometwin -f` |
| Restart after config edit | `systemctl --user restart hometwin` |
| Update | `~/.local/share/hometwin/update.sh`, the dashboard's Update button, or re-run install.sh |
| Stop | `systemctl --user stop hometwin` |

The dashboard's Restart/Update/Shutdown buttons work under systemd:
the process exits cleanly (state saved) and `Restart=always` brings it
back up. The unit carries `HOMETWIN_REPO`/`HOMETWIN_CHANNEL`, so the
in-dashboard update knows where to pull from.

## GPU bring-up (OcuLink 4060 Ti)

1. NVIDIA driver: `sudo apt install nvidia-driver-550` (or distro
   equivalent); reboot; `nvidia-smi` must list the 4060 Ti. OcuLink is
   transparent here — the GPU appears as ordinary PCIe; just attach it
   before boot (no hotplug).
2. `onnxruntime-gpu` (installed by `HOMETWIN_GPU=1`) bundles the CUDA EP;
   it needs the CUDA runtime libraries — `sudo apt install
   nvidia-cuda-toolkit` or pip's `nvidia-cuda-runtime-cu12` wheels
   depending on ORT version.
3. Verify from the running tracker:
   `curl -s localhost:8080/health` → `accel.onnx_providers` should list
   `CUDAExecutionProvider` ahead of CPU. The accel layer auto-orders
   TensorRT → CUDA → CPU; nothing to configure.
4. TensorRT is an optional later step (faster, slower first-start due to
   engine builds) — install `tensorrt` libs and ORT picks it up.

Until ONNX detectors are in play (RT-DETRv2 etc.), the GPU is idle —
ArUco tag detection is CPU OpenCV and doesn't need it.

## Server notes

- **Webcams**: the service user needs video access for local USB
  cameras: `sudo usermod -aG video $USER` and re-login. Network cameras
  (MJPEG/RTSP URLs as `device:`) need nothing.
- **API exposure**: keep `api.host: 127.0.0.1` and put caddy/nginx with
  TLS in front for off-box access (docs/security.md); enable bearer
  tokens when leaving loopback.
- **Home Assistant / MQTT and the node bridge** (`network_bridge`, port
  8787) work as on any host — open that port to the IoT VLAN only.
- The Windows/macOS launchers and this server can share the repo and
  channel: every install self-updates from the same branch.
