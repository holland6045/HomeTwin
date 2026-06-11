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
                self._send(200, {"status": "ok", "sensors": len(tracker.sensors)})
                return
            if not self._authorize(write=False):
                return
            if parts == ["assets", "splat"]:
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
