"""HTTP API over the tracker. Stdlib only — runs anywhere the core runs.

GET  /                 -> visualization dashboard (map + camera overlays)
GET  /health           -> {"status": "ok", ...}
GET  /items            -> all item estimates
GET  /items/<query>    -> one item by id or name (404 if unknown)
GET  /presence         -> latest occupancy estimate (tomography etc.)
GET  /people           -> tracked occupants (smoothed position+velocity)
GET  /events           -> recent zone-change events, oldest first
GET  /overlay/map      -> world-space layers for the top-down map view
GET  /overlay/camera/<sensor_id> -> same layers projected into camera pixels
GET  /camera/<sensor_id>/frame.jpg -> latest captured frame (?annotate=1
                          draws detection boxes)
GET  /camera/<sensor_id>/stream.mjpg -> live MJPEG stream of captured
                          frames — the dashboard's camera view when no
                          external stream_url is configured
GET  /debug/bundle     -> zip of everything needed to debug remotely:
                          health, overlays, items, events, per-camera
                          state + annotated frames
GET  /config/cameras   -> capture settings per camera (requested vs
                          negotiated mode)
GET  /config/cameras/<sensor_id>/modes -> capture modes the webcam supports
GET  /make-tag?id=&ident=&caption=&size_mm=&layout= -> printable tag SVG
POST /config/cameras/<sensor_id> -> {device?, width?, height?, fps?,
                          fourcc?} hot-swap capture settings; persisted
                          to <config>.overrides.json
POST /config/cameras/<sensor_id>/detector -> {enable_ai: true} (download
                          RT-DETR + compose onto the detector) or
                          {detector: {...}}; persisted
POST /system/restart   -> respawn the tracker process (state saved)
POST /system/update    -> pip-reinstall hometwin from the update channel
                          (HOMETWIN_REPO/HOMETWIN_CHANNEL env), then respawn
POST /system/shutdown  -> save state and exit
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


def _has_ai(detector_cfg) -> bool:
    """True if the camera's detector runs an ONNX object detector."""
    if not detector_cfg:
        return False
    t = detector_cfg.get("type")
    if t in ("rtdetr", "onnx"):
        return True
    if t == "multi":
        return any(_has_ai(d) for d in detector_cfg.get("detectors", []))
    return False


def _detector_summary(detector_cfg) -> str:
    if not detector_cfg:
        return "unknown"
    t = detector_cfg.get("type")
    if t == "multi":
        return "+".join(d.get("type", "?") for d in detector_cfg.get("detectors", []))
    return t or "unknown"


def _exit_process(tracker, server, relaunch: bool) -> None:
    """Stop everything cleanly, optionally respawn this same command, exit.
    Runs on its own thread after the HTTP response has been sent."""
    import shutil
    import subprocess
    import sys
    import time

    time.sleep(0.5)  # let the response reach the client
    try:
        tracker.shutdown()
        time.sleep(2.0)  # run() finally: saves state, releases cameras
    except Exception:
        pass
    try:
        server.shutdown()
        server.server_close()  # free the port for the respawned process
    except Exception:
        pass
    if relaunch:
        exe = sys.argv[0] if os.path.exists(sys.argv[0]) else shutil.which(sys.argv[0])
        if exe:
            kwargs = (
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: survive parent exit
                {"creationflags": 0x00000208} if os.name == "nt"
                else {"start_new_session": True}
            )
            subprocess.Popen([exe, *sys.argv[1:]], **kwargs)
    os._exit(0)


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
            elif len(parts) == 3 and parts[0] == "camera" and parts[2] == "depth.jpg":
                self._depth_jpg(parts[1])
            elif len(parts) == 3 and parts[0] == "camera" and parts[2] == "stream.mjpg":
                self._stream_mjpg(parts[1])
            elif parts == ["debug", "bundle"]:
                self._debug_bundle()
            elif parts == ["overlay", "map"]:
                self._send(200, map_overlay(tracker))
            elif len(parts) == 3 and parts[:2] == ["overlay", "camera"]:
                data = camera_overlay(tracker, parts[2])
                self._send(200, data) if data else self._send(404, {"error": "unknown camera"})
            elif parts == ["config", "cameras"]:
                self._send(200, [
                    {
                        "sensor_id": s.sensor_id,
                        "stream_url": s.stream_url,
                        "capture": (s.frame_source.describe()
                                    if hasattr(s.frame_source, "describe") else None),
                        "detector": _detector_summary(getattr(s, "detector_cfg", None)),
                        "ai": _has_ai(getattr(s, "detector_cfg", None)),
                        "depth": s.depth_scale.status() if getattr(s, "depth", None) else None,
                    }
                    for s in tracker.sensors if hasattr(s, "frame_source")
                ])
            elif len(parts) == 4 and parts[:2] == ["config", "cameras"] and parts[3] == "modes":
                cam = next((s for s in tracker.sensors
                            if getattr(s, "sensor_id", None) == parts[2]
                            and hasattr(s, "probe_modes")), None)
                if cam is None:
                    self._send(404, {"error": "unknown camera"})
                else:
                    self._send(200, {"modes": cam.probe_modes()})
            elif parts == ["make-tag"]:
                self._make_tag(parse_qs(urlsplit(self.path).query))
            elif parts == ["items"]:
                self._send(200, tracker.snapshot())
            elif len(parts) == 2 and parts[0] == "items":
                entry = tracker.find(parts[1])
                self._send(200, entry) if entry else self._send(404, {"error": "unknown item"})
            elif parts == ["presence"]:
                self._send(200, tracker.presence or {"status": "no_data"})
            elif parts == ["people"]:
                self._send(200, list(tracker.people))
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

        def _depth_jpg(self, sensor_id: str) -> None:
            """Colorized snapshot of a camera's latest monocular-depth map."""
            cam = next((s for s in tracker.sensors
                        if getattr(s, "sensor_id", None) == sensor_id), None)
            dmap = getattr(cam, "_depth_map", None) if cam else None
            if dmap is None:
                self._send(404, {"error": "no depth map (depth not enabled / no keyframe yet)"})
                return
            try:
                import cv2
                import numpy as np
            except ImportError:
                self._send(503, {"error": "opencv not installed"})
                return
            lo, hi = float(dmap.min()), float(dmap.max())
            norm = (dmap - lo) / (hi - lo + 1e-6)
            u8 = (norm * 255).astype(np.uint8)
            color = cv2.applyColorMap(u8, cv2.COLORMAP_INFERNO)
            # the depth map is square (model resolution); stretch it back to
            # the frame's aspect so it overlays the video 1:1 under letterbox
            frame = getattr(cam, "last_frame", None)
            if frame is not None:
                color = cv2.resize(color, (frame.shape[1], frame.shape[0]))
            ok, buf = cv2.imencode(".jpg", color, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                self._send(500, {"error": "JPEG encode failed"})
                return
            self._send_bytes(buf.tobytes(), "image/jpeg")

        def _make_tag(self, qs: dict) -> None:
            """Printable ArUco label SVG — the in-app tag maker. ?id=7&
            ident=KEY/01&caption=keys&size_mm=70&layout=square|wide&palette=…"""
            try:
                from hometwin.tags import marker_bits, tag_svg
            except Exception as e:
                self._send(503, {"error": f"tag maker unavailable: {e}"})
                return
            try:
                mid = int(qs.get("id", ["0"])[0])
            except ValueError:
                self._send(400, {"error": "id must be an integer"})
                return
            dictionary = qs.get("dictionary", ["DICT_4X4_250"])[0]
            try:
                bits = marker_bits(dictionary, mid)
                twin_bits = None
                if qs.get("twin_id"):
                    twin_bits = marker_bits(dictionary, int(qs["twin_id"][0]))
                svg = tag_svg(
                    bits, qs.get("ident", [f"TAG/{mid:02d}"])[0],
                    caption=qs.get("caption", [""])[0],
                    palette=qs.get("palette", ["signal"])[0],
                    size_mm=float(qs.get("size_mm", ["70"])[0]),
                    layout=qs.get("layout", ["portrait"])[0],
                    twin=qs.get("twin", ["false"])[0] in ("1", "true"),
                    twin_bits=twin_bits,
                )
            except (RuntimeError, ValueError, KeyError) as e:
                self._send(400, {"error": str(e)})
                return
            self._send_bytes(svg.encode(), "image/svg+xml",
                             f'inline; filename="tag-{mid}.svg"')

        def _stream_mjpg(self, sensor_id: str) -> None:
            """Multipart MJPEG of the camera's retained frames. Runs at the
            tracker's capture cadence (capped ~20 fps); unchanged frames are
            re-sent at 2 fps so the connection never looks dead. Ends when
            the client disconnects — each viewer costs one handler thread."""
            import time

            cam = next((s for s in tracker.sensors
                        if getattr(s, "sensor_id", None) == sensor_id
                        and hasattr(s, "last_frame")), None)
            if cam is None:
                self._send(404, {"error": "unknown camera"})
                return
            try:
                import cv2
            except ImportError:
                self._send(503, {"error": "opencv not installed"})
                return
            boundary = "hometwinframe"
            self.send_response(200)
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={boundary}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last_sent_ts = -1.0
            last_sent_at = 0.0
            try:
                while True:
                    frame, ts = cam.last_frame, cam.last_frame_ts
                    now = time.monotonic()
                    if frame is None or (ts == last_sent_ts and now - last_sent_at < 0.5):
                        time.sleep(0.02)
                        continue
                    # draw detection boxes so "did it find the tag" is obvious
                    # regardless of camera-pose calibration
                    shown = _annotate(frame, cam.last_detections) if cam.last_detections else frame
                    ok, buf = cv2.imencode(".jpg", shown,
                                           [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok:
                        body = buf.tobytes()
                        self.wfile.write(
                            f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(body)}\r\n\r\n".encode())
                        self.wfile.write(body)
                        self.wfile.write(b"\r\n")
                    last_sent_ts, last_sent_at = ts, now
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # viewer closed the tab

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
                        "capture": (s.frame_source.describe()
                                    if hasattr(s.frame_source, "describe") else None),
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
                # log tails: the last crash lands here even when it killed a
                # previous run (logs persist across restarts)
                from glob import glob

                for lp in {"logs/tracker.log", "logs/tracker.err.log",
                           "logs/tracker.out.log", *glob("logs/*.log")}:
                    try:
                        with open(lp, "rb") as f:
                            f.seek(0, 2)
                            f.seek(max(0, f.tell() - 200_000))
                            z.writestr(f"logs/{os.path.basename(lp)}",
                                       f.read().decode("utf-8", "replace"))
                    except OSError:
                        pass
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
            elif len(parts) == 4 and parts[:2] == ["config", "cameras"] and parts[3] == "detector":
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "invalid JSON"})
                    return
                try:
                    if body.get("enable_ai"):
                        from hometwin.models import download_model

                        path = str(download_model("rtdetr", body.get("variant", "fp32")))
                        cfg = tracker.enable_ai_detection(parts[2], path)
                    elif "detector" in body:
                        tracker.reconfigure_detector(parts[2], body["detector"])
                        cfg = body["detector"]
                    else:
                        self._send(400, {"error": "need {enable_ai: true} or {detector: {...}}"})
                        return
                except KeyError as e:
                    self._send(404, {"error": str(e.args[0])})
                    return
                except Exception as e:
                    self._send(500, {"error": str(e)})
                    return
                self._send(200, {"status": "applied", "detector": cfg})
            elif len(parts) == 3 and parts[:2] == ["config", "cameras"]:
                length = int(self.headers.get("Content-Length", 0))
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    src = {}
                    dev = body.get("device", "")
                    if dev != "" and dev is not None:
                        src["device"] = int(dev) if str(dev).isdigit() else str(dev)
                    for k, cast in (("width", int), ("height", int), ("fps", float)):
                        if body.get(k) not in ("", None):
                            src[k] = cast(body[k])
                    if "fourcc" in body:
                        src["fourcc"] = str(body["fourcc"]).upper() or None
                except (TypeError, ValueError, json.JSONDecodeError):
                    self._send(400, {"error": "invalid camera settings"})
                    return
                try:
                    capture = tracker.reconfigure_camera(parts[2], src)
                except KeyError as e:
                    self._send(404, {"error": str(e.args[0])})
                    return
                except Exception as e:
                    self._send(500, {"error": f"camera rejected these settings: {e}"})
                    return
                self._send(200, {"status": "applied", "capture": capture})
            elif len(parts) == 2 and parts[0] == "system" and parts[1] in (
                    "restart", "update", "shutdown"):
                self._system(parts[1])
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

        def _system(self, action: str) -> None:
            import subprocess
            import sys

            if action == "update":
                repo = os.environ.get("HOMETWIN_REPO")
                channel = os.environ.get("HOMETWIN_CHANNEL")
                if not repo or not channel:
                    self._send(400, {"error": "update channel unknown "
                                              "(HOMETWIN_REPO/HOMETWIN_CHANNEL unset) — "
                                              "relaunch the app to update instead"})
                    return
                spec = f"hometwin @ git+{repo}@{channel}"
                pip = [sys.executable, "-m", "pip", "install", "--upgrade",
                       "--force-reinstall", "--no-deps", spec]
                if os.name == "nt":
                    # Windows can't overwrite the running hometwin.exe, so hand
                    # off to a detached python that waits for us to exit, then
                    # installs and relaunches the same command.
                    relaunch = [sys.argv[0], *sys.argv[1:]]
                    code = ("import time,subprocess;time.sleep(3);"
                            f"subprocess.run({pip!r});"
                            f"subprocess.Popen({relaunch!r})")
                    subprocess.Popen([sys.executable, "-c", code], creationflags=0x00000208)
                    self._send(200, {"status": "update",
                                     "note": "updating in the background — reconnecting shortly"})
                    threading.Thread(target=_exit_process,
                                     args=(tracker, self.server, False), daemon=True).start()
                    return
                proc = subprocess.run(pip, capture_output=True, text=True, timeout=600)
                if proc.returncode != 0:
                    self._send(500, {"error": "pip install failed",
                                     "log": (proc.stdout + proc.stderr)[-2000:]})
                    return
            self._send(200, {"status": action})
            threading.Thread(
                target=_exit_process,
                args=(tracker, self.server, action != "shutdown"),
                daemon=True,
            ).start()

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
