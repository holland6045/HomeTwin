"""HTTP API over the tracker. Stdlib only — runs anywhere the core runs.

GET  /health           -> {"status": "ok", ...}
GET  /items            -> all item estimates
GET  /items/<query>    -> one item by id or name (404 if unknown)
GET  /presence         -> latest occupancy estimate (tomography etc.)
POST /items/<id>/tags  -> {"tag": "ble:AA:.."} manual tagging at runtime
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from apartment_tracker.tracker import Tracker


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
            if parts == ["health"]:
                self._send(200, {"status": "ok", "sensors": len(tracker.sensors)})
            elif parts == ["items"]:
                self._send(200, tracker.snapshot())
            elif len(parts) == 2 and parts[0] == "items":
                entry = tracker.find(parts[1])
                self._send(200, entry) if entry else self._send(404, {"error": "unknown item"})
            elif parts == ["presence"]:
                self._send(200, tracker.presence or {"status": "no_data"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            parts = [unquote(p) for p in self.path.split("?")[0].split("/") if p]
            if len(parts) == 3 and parts[0] == "items" and parts[2] == "tags":
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
