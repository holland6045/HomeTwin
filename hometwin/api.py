"""HTTP API over the tracker. Stdlib only — runs anywhere the core runs.

GET  /                 -> visualization dashboard (map + camera overlays)
GET  /health           -> {"status": "ok", ...}
GET  /items            -> all item estimates
GET  /items/<query>    -> one item by id or name (404 if unknown)
GET  /presence         -> latest occupancy estimate (tomography etc.)
GET  /events           -> recent zone-change events, oldest first
GET  /overlay/map      -> world-space layers for the top-down map view
GET  /overlay/camera/<sensor_id> -> same layers projected into camera pixels
GET  /camera/<sensor_id>/frame.jpg -> latest captured frame (?annotate=1
                          draws detection boxes) — the dashboard's camera
                          view when no external stream_url is configured
GET  /debug/bundle     -> zip of everything needed to debug remotely:
                          health, overlays, items, events, per-camera
                          state + annotated frames
POST /items/<id>/tags  -> {"tag": "ble:AA:.."} manual tagging at runtime
POST /anchors          -> {"tag", "position"} drop a calibration anchor at
                          runtime (map-click coords / board-check output)
POST /motion-zones     -> {"name", min/max | center/radius} create a synthetic
                          motion-sensor zone (announced to Home Assistant)
POST /assets/splat     -> replace the splat scan (raw body); atomic, no restart
POST /assets/splat/transform -> update world.splat_transform at runtime

Authentication (`api.auth.tokens` in config): bearer tokens with two roles.
`viewer` reads, `admin` reads and writes. One constant-time comparison per
request — no measurable overhead. With no tokens configured the API stays
open for reads but restricts writes to loopback peers. `/` and `/health`
are always served (the dashboard loads, then prompts for a token on the
first 401). GET requests also accept `?token=` for clients that cannot set
headers (the splat viewer); avoid it for admin tokens — query strings end
up in logs. TLS belongs in a reverse proxy (caddy/nginx), not in-process.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from hometwin.overlay import camera_overlay, map_overlay
from hometwin.tracker import Tracker

UPLOAD_CHUNK = 1 << 20
MAX_UPLOAD = 2 << 30  # splat scans are large, but bound the write anyway
LOOPBACK = ("127.0.0.1", "::1")


class AuthPolicy:
    def __init__(self, tokens: list[dict] | None):
        self._tokens = [(t["token"], t.get("role", "admin")) for t in (tokens or [])]

    @property
    def enabled(self) -> bool:
        return bool(self._tokens)

    def role(self, presented: str | None) -> str | None:
        if not presented:
            return None
        granted = None
        for secret, role in self._tokens:  # check all: no early-exit timing signal
            if hmac.compare_digest(secret, presented):
                granted = role
        return granted


def _ui_html() -> bytes:
    return (resources.files("hometwin") / "static" / "ui.html").read_bytes()


def _annotate(frame, detections):
    import cv2

    img = frame.copy()
    h, w = img.shape[:2]
    for det in detections:
        x, y, bw, bh = det.bbox
        p1 = (int(x * w), int(y * h))
        p2 = (int((x + bw) * w), int((y + bh) * h))
        cv2.rectangle(img, p1, p2, (80, 220, 80), 2)
        text = f"{det.tag_id or det.label} {det.confidence:.2f}"
        cv2.putText(img, text, (p1[0], max(p1[1] - 6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 220, 80), 1, cv2.LINE_AA)
    return img


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("hometwin")
    except Exception:
        return "unknown"


def make_handler(tracker: Tracker, policy: AuthPolicy):
    class Handler(BaseHTTPRequestHandler):
        def _presented_token(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
            qs = parse_qs(urlsplit(self.path).query)
            return qs["token"][0] if "token" in qs else None

        def _authorize(self, write: bool) -> bool:
            if not policy.enabled:
                if write and self.client_address[0] not in LOOPBACK:
                    self._send(
                        403,
                        {"error": "writes restricted to localhost (no auth configured)"},
                    )
                    return False
                return True
            role = policy.role(self._presented_token())
            if role is None:
                body = json.dumps({"error": "authentication required"}).encode()
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="hometwin"')
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False
            if write and role != "admin":
                self._send(403, {"error": "admin token required"})
                return False
            return True

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
            if parts in ([], ["ui"]):
                body = _ui_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parts == ["health"]:
                from hometwin.accel import capabilities

                self._send(200, {
                    "status": "ok",
                    "sensors": len(tracker.sensors),
                    "accel": capabilities(),
                })
                return
            if not self._authorize(write=False):
                return
            if parts == ["assets", "floorplan"]:
                fp = getattr(tracker.cfg, "floorplan", None) or {}
                try:
                    with open(fp.get("image", ""), "rb") as f:
                        body = f.read()
                except (TypeError, OSError):
                    self._send(404, {"error": "no floorplan configured"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parts == ["assets", "splat"]:
                path = getattr(tracker.cfg, "splat_asset", None)
                try:
                    f = open(path, "rb")
                except (TypeError, OSError):
                    self._send(404, {"error": "no splat asset configured"})
                    return
                with f:
                    size = os.fstat(f.fileno()).st_size
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    # scans run to hundreds of MB: stream, never buffer
                    while chunk := f.read(UPLOAD_CHUNK):
                        self.wfile.write(chunk)
            elif parts == ["pointcloud"]:
                wm = tracker.worldmodel
                self._send(200, {
                    **wm.stats(),
                    "points": wm.point_cloud(),
                })
            elif len(parts) == 3 and parts[0] == "camera" and parts[2] == "frame.jpg":
                self._frame_jpg(parts[1])
            elif parts == ["debug", "bundle"]:
                self._debug_bundle()
            elif parts == ["overlay", "map"]:
                self._send(200, map_overlay(tracker))
            elif len(parts) == 3 and parts[:2] == ["overlay", "camera"]:
                data = camera_overlay(tracker, parts[2])
                self._send(200, data) if data else self._send(404, {"error": "unknown camera"})
            elif parts == ["items"]:
                self._send(200, tracker.snapshot())
            elif len(parts) == 2 and parts[0] == "items":
                entry = tracker.find(parts[1])
                self._send(200, entry) if entry else self._send(404, {"error": "unknown item"})
            elif parts == ["presence"]:
                self._send(200, tracker.presence or {"status": "no_data"})
            elif parts == ["events"]:
                self._send(200, list(tracker.events))
            else:
                self._send(404, {"error": "not found"})

        def _send_bytes(self, body: bytes, ctype: str, disposition: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _frame_jpg(self, sensor_id: str) -> None:
            cam = next((s for s in tracker.sensors
                        if getattr(s, "sensor_id", None) == sensor_id
                        and hasattr(s, "last_frame")), None)
            if cam is None:
                self._send(404, {"error": "unknown camera"})
                return
            frame = cam.last_frame
            if frame is None:
                self._send(404, {"error": "no frame captured yet"})
                return
            try:
                import cv2
            except ImportError:
                self._send(503, {"error": "opencv not installed"})
                return
            qs = parse_qs(urlsplit(self.path).query)
            if qs.get("annotate", ["0"])[0] not in ("0", "", "false"):
                frame = _annotate(frame, cam.last_detections)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                self._send(500, {"error": "JPEG encode failed"})
                return
            self._send_bytes(buf.tobytes(), "image/jpeg")

        def _debug_bundle(self) -> None:
            """Everything needed to debug a live install from one zip,
            without remote access: state snapshots + annotated frames.
            Every section is best-effort — a broken subsystem becomes an
            .error.txt entry instead of killing the bundle."""
            import io
            import platform
            import sys
            import time
            import zipfile
            from dataclasses import asdict

            from hometwin.accel import capabilities

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                def add_json(name, build):
                    try:
                        z.writestr(name, json.dumps(build(), indent=2, default=str))
                    except Exception as e:
                        z.writestr(name + ".error.txt", repr(e))

                add_json("health.json", lambda: {
                    "time": time.time(),
                    "version": _version(),
                    "python": sys.version,
                    "platform": platform.platform(),
                    "sensors": [
                        {"id": getattr(s, "sensor_id", None), "type": type(s).__name__}
                        for s in tracker.sensors
                    ],
                    "accel": capabilities(),
                })
                add_json("overlay_map.json", lambda: map_overlay(tracker))
                add_json("items.json", tracker.snapshot)
                add_json("events.json", lambda: list(tracker.events))
                add_json("presence.json", lambda: tracker.presence)
                for s in tracker.sensors:
                    sid = getattr(s, "sensor_id", None)
                    if sid is None or not hasattr(s, "last_frame"):
                        continue
                    frame = s.last_frame
                    add_json(f"camera_{sid}.json", lambda s=s, frame=frame: {
                        "has_frame": frame is not None,
                        "frame_shape": getattr(frame, "shape", None),
                        "last_frame_ts": s.last_frame_ts,
                        "stream_url": s.stream_url,
                        "detections": [asdict(d) for d in s.last_detections],
                        "overlay": camera_overlay(tracker, sid),
                    })
                    if frame is None:
                        continue
                    try:
                        import cv2

                        ok, jb = cv2.imencode(".jpg", _annotate(frame, s.last_detections))
                        if ok:
                            z.writestr(f"camera_{sid}.jpg", jb.tobytes())
                    except Exception as e:
                        z.writestr(f"camera_{sid}.jpg.error.txt", repr(e))
            self._send_bytes(
                buf.getvalue(), "application/zip",
                f'attachment; filename="hometwin-diag-{int(time.time())}.zip"',
            )

        def do_POST(self) -> None:
            parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
            if not self._authorize(write=True):
                return
            if parts == ["assets", "splat"]:
                path = getattr(tracker.cfg, "splat_asset", None)
                if not path:
                    self._send(400, {"error": "no splat_asset path configured"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    self._send(400, {"error": "empty body"})
                    return
                if length > MAX_UPLOAD:
                    self._send(413, {"error": "upload too large"})
                    return
                dest = Path(path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                written = 0
                with open(tmp, "wb") as f:
                    while written < length:
                        chunk = self.rfile.read(min(UPLOAD_CHUNK, length - written))
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                if written != length:
                    tmp.unlink(missing_ok=True)
                    self._send(400, {"error": "truncated upload"})
                    return
                os.replace(tmp, dest)
                self._send(200, {"status": "updated", "bytes": written})
            elif parts == ["assets", "splat", "transform"]:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid JSON"})
                    return
                tracker.cfg.splat_transform = body or None
                self._send(200, {"status": "updated", "note": "runtime only — persist in config"})
            elif parts == ["motion-zones"]:
                if tracker.cfg.motion_zones is None:
                    from hometwin.hass import MotionZoneController

                    tracker.cfg.motion_zones = MotionZoneController()
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    from hometwin.hass import MotionZone

                    zone = MotionZone(
                        str(body["name"]),
                        min_corner=body.get("min"),
                        max_corner=body.get("max"),
                        center=body.get("center"),
                        radius=body.get("radius"),
                        off_delay_s=float(body.get("off_delay_s", 30.0)),
                    )
                    tracker.cfg.motion_zones.add(zone)
                except (KeyError, TypeError, ValueError) as e:
                    self._send(400, {"error": str(e)})
                    return
                if tracker.cfg.hass is not None:
                    tracker.cfg.hass.announce(zone)
                self._send(200, {"status": "created", "slug": zone.slug,
                                 "note": "runtime only — persist in config"})
            elif parts == ["anchors"]:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    tag = str(body["tag"])
                    position = tuple(float(v) for v in body["position"])
                    if len(position) != 3:
                        raise ValueError("position must have 3 components")
                except (KeyError, TypeError, ValueError):
                    self._send(400, {"error": "need {tag, position: [x,y,z]}"})
                    return
                tracker.add_anchor(tag, position)
                self._send(200, {"status": "anchored", "tag": tag})
            elif len(parts) == 3 and parts[0] == "items" and parts[2] == "tags":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    tracker.tag_item(parts[1], str(body["tag"]))
                except KeyError:
                    self._send(400, {"error": "missing 'tag'"})
                    return
                except Exception as e:
                    self._send(400, {"error": str(e)})
                    return
                self._send(200, {"status": "tagged"})
            else:
                self._send(404, {"error": "not found"})

        def log_message(self, *args) -> None:
            pass

    return Handler


class ApiServer:
    def __init__(self, tracker: Tracker, host: str = "127.0.0.1", port: int = 8080):
        policy = AuthPolicy(getattr(tracker.cfg, "api_tokens", None))
        self._server = ThreadingHTTPServer((host, port), make_handler(tracker, policy))
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
