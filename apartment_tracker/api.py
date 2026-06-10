"""HTTP API over the tracker. Stdlib only — runs anywhere the core runs.

GET  /                 -> visualization dashboard (map + camera overlays)
GET  /health           -> {"status": "ok", ...}
GET  /items            -> all item estimates
GET  /items/<query>    -> one item by id or name (404 if unknown)
GET  /presence         -> latest occupancy estimate (tomography etc.)
GET  /events           -> recent zone-change events, oldest first
GET  /overlay/map      -> world-space layers for the top-down map view
GET  /overlay/camera/<sensor_id> -> same layers projected into camera pixels
POST /items/<id>/tags  -> {"tag": "ble:AA:.."} manual tagging at runtime
POST /assets/splat     -> replace the splat scan (raw body); atomic, no restart
POST /assets/splat/transform -> update world.splat_transform at runtime

The API binds to 127.0.0.1 by default; the splat upload endpoint is
unauthenticated, so put a reverse proxy with auth in front before exposing
it beyond localhost.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote

from apartment_tracker.overlay import camera_overlay, map_overlay
from apartment_tracker.tracker import Tracker

UPLOAD_CHUNK = 1 << 20


def _ui_html() -> bytes:
    return (resources.files("apartment_tracker") / "static" / "ui.html").read_bytes()


def make_handler(tracker: Tracker):
    class Handler(BaseHTTPRequestHandler):
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
            elif parts == ["assets", "splat"]:
                path = getattr(tracker.cfg, "splat_asset", None)
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                except (TypeError, OSError):
                    self._send(404, {"error": "no splat asset configured"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parts == ["overlay", "map"]:
                self._send(200, map_overlay(tracker))
            elif len(parts) == 3 and parts[:2] == ["overlay", "camera"]:
                data = camera_overlay(tracker, parts[2])
                self._send(200, data) if data else self._send(404, {"error": "unknown camera"})
            elif parts == ["health"]:
                self._send(200, {"status": "ok", "sensors": len(tracker.sensors)})
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

        def do_POST(self) -> None:
            parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
            if parts == ["assets", "splat"]:
                path = getattr(tracker.cfg, "splat_asset", None)
                if not path:
                    self._send(400, {"error": "no splat_asset path configured"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0:
                    self._send(400, {"error": "empty body"})
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
        self._server = ThreadingHTTPServer((host, port), make_handler(tracker))
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
