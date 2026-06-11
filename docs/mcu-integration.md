# MCU node integration

Any microcontroller becomes a tracker sensor by opening a TCP connection to
the `network_bridge` sensor (default port 8787) and writing one JSON object
per line. No registration, no schema negotiation, no firmware coupling —
the contract below is the whole interface.

## Message contract

```json
{"type": "rssi", "sensor_id": "esp32-hall", "mac": "AA:BB:CC:DD:EE:FF",
 "anchor": [0.0, 4.0, 2.2], "rssi": -67}

{"type": "range", "sensor_id": "uwb-1", "item": "ble:AA:BB:CC:DD:EE:FF",
 "anchor": [0.0, 4.0, 2.2], "range_m": 2.1, "sigma_m": 0.3}

{"type": "position", "sensor_id": "cam-2", "item": "aruco:7",
 "pos": [1.2, 3.4, 0.9], "sigma_m": 0.3}

{"type": "area", "sensor_id": "rti-mesh", "label": "presence",
 "centroid": [2.0, 2.0, 1.0], "sigma_m": 1.5}
```

Rules the bridge enforces (`hometwin/sensors/network.py`):

- Timestamps are assigned host-side on receipt; MCU clocks are never trusted.
- Malformed lines are counted and dropped — a flaky node cannot take the
  tracker down.
- `rssi` messages are converted to ranges host-side using the trainable
  path-loss model, so recalibrating BLE never means reflashing firmware.
- Coordinates are world-frame meters (z up). The node's `anchor` is its own
  mounted position — the only piece of world knowledge a node carries.

## Reference firmware

`firmware/esp32-ble-scanner/` is a complete PlatformIO node: ESP32 +
NimBLE continuous scan, every advertisement forwarded as an `rssi` line.
Identity (`SENSOR_ID`) and mounting position (`ANCHOR_*`) are build flags,
Wi-Fi credentials come from the environment:

```bash
WIFI_SSID=home WIFI_PASS=secret pio run -e esp32-ble-scanner -t upload
```

It is a reference, not a product: verify the NimBLE pin/partition setup for
your specific board before deploying.

## Node design guidance

- Keep nodes dumb. Format conversion, filtering, and fusion all live
  host-side, so nodes are cheap to write, port, and replace.
- One TCP connection per node; reconnect with backoff on drop. The bridge
  is threaded and handles many concurrent nodes.
- An ESP32-CAM does not need this bridge at all: serve MJPEG and let the
  host's `camera` sensor pull frames (`source: {type: opencv, device:
  "http://<node>/mjpeg"}`) — detection runs host-side where the compute is.
- RF tomography meshes report per-link RSS however is convenient (one
  gateway node forwarding `{"type": "area"}` after local reconstruction, or
  raw link data to a custom host-side `LinkSource`).
