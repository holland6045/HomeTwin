"""Auth: API bearer tokens/roles, loopback fallback, bridge connection auth."""

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from apartment_tracker.api import ApiServer
from apartment_tracker.config import resolve_tokens
from apartment_tracker.sensors.network import NetworkBridgeSensor
from apartment_tracker.simulate import build_simulation

ADMIN = "admin-secret-token"
VIEWER = "viewer-secret-token"


def serve(tokens=None):
    tracker, _ = build_simulation(seed=1)
    if tokens is not None:
        tracker.cfg.api_tokens = tokens
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    return tracker, api, f"http://127.0.0.1:{api.port}"


def get(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)


def post(url, data=b"{}", token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, method="POST", headers=headers), timeout=5
    )


def status_of(fn):
    try:
        with fn() as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# --- no tokens configured: open reads, loopback-only writes -------------------

def test_no_auth_reads_open_writes_allowed_from_loopback():
    tracker, api, base = serve()
    try:
        assert status_of(lambda: get(f"{base}/items")) == 200
        assert status_of(
            lambda: post(f"{base}/items/keys/tags", json.dumps({"tag": "x:1"}).encode())
        ) == 200
    finally:
        api.stop()


# --- tokens configured ---------------------------------------------------------

def auth_tokens():
    return [{"token": ADMIN, "role": "admin"}, {"token": VIEWER, "role": "viewer"}]


def test_reads_require_token():
    tracker, api, base = serve(auth_tokens())
    try:
        assert status_of(lambda: get(f"{base}/items")) == 401
        assert status_of(lambda: get(f"{base}/items", token="wrong")) == 401
        assert status_of(lambda: get(f"{base}/items", token=VIEWER)) == 200
        assert status_of(lambda: get(f"{base}/items", token=ADMIN)) == 200
        # dashboard shell and health stay reachable so the login flow works
        assert status_of(lambda: get(f"{base}/")) == 200
        assert status_of(lambda: get(f"{base}/health")) == 200
    finally:
        api.stop()


def test_writes_require_admin_role():
    tracker, api, base = serve(auth_tokens())
    try:
        body = json.dumps({"tag": "ble:11:22:33:44:55:66"}).encode()
        assert status_of(lambda: post(f"{base}/items/keys/tags", body)) == 401
        assert status_of(lambda: post(f"{base}/items/keys/tags", body, token=VIEWER)) == 403
        assert status_of(lambda: post(f"{base}/items/keys/tags", body, token=ADMIN)) == 200
    finally:
        api.stop()


def test_query_param_token_for_get():
    tracker, api, base = serve(auth_tokens())
    try:
        assert status_of(lambda: get(f"{base}/overlay/map?token={VIEWER}")) == 200
        assert status_of(lambda: get(f"{base}/overlay/map?token=bogus")) == 401
    finally:
        api.stop()


def test_splat_upload_respects_roles(tmp_path):
    tracker, api, base = serve(auth_tokens())
    tracker.cfg.splat_asset = str(tmp_path / "scan.ply")
    try:
        assert status_of(lambda: post(f"{base}/assets/splat", b"scan", token=VIEWER)) == 403
        assert status_of(lambda: post(f"{base}/assets/splat", b"scan", token=ADMIN)) == 200
        assert (tmp_path / "scan.ply").read_bytes() == b"scan"
    finally:
        api.stop()


# --- token resolution ----------------------------------------------------------

def test_resolve_tokens_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TRK_TOK", "from-env")
    tok_file = tmp_path / "viewer.token"
    tok_file.write_text("from-file\n")
    tokens = resolve_tokens(
        {
            "tokens": [
                {"env": "TRK_TOK", "role": "admin"},
                {"file": str(tok_file), "role": "viewer"},
                {"token": "literal"},
            ]
        }
    )
    assert tokens == [
        {"token": "from-env", "role": "admin"},
        {"token": "from-file", "role": "viewer"},
        {"token": "literal", "role": "admin"},
    ]


def test_resolve_tokens_rejects_bad_specs(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ValueError):
        resolve_tokens({"tokens": [{"env": "MISSING_VAR"}]})
    with pytest.raises(ValueError):
        resolve_tokens({"tokens": [{"token": "x", "role": "superuser"}]})
    with pytest.raises(ValueError):
        resolve_tokens({"tokens": [{"role": "admin"}]})


# --- network bridge connection auth ---------------------------------------------

def bridge_send(port, lines):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        for line in lines:
            sock.sendall(line.encode() + b"\n")
        time.sleep(0.2)


def wait_obs(bridge, timeout=3.0):
    deadline = time.time() + timeout
    obs = []
    while not obs and time.time() < deadline:
        obs = bridge.poll()
        time.sleep(0.02)
    return obs


POS_MSG = json.dumps({"type": "position", "item": "aruco:7", "pos": [1, 1, 1]})


def test_bridge_accepts_authenticated_connection():
    bridge = NetworkBridgeSensor("bridge", host="127.0.0.1", port=0, auth_token="hunter2")
    bridge.start()
    try:
        bridge_send(bridge.port, [json.dumps({"auth": "hunter2"}), POS_MSG])
        obs = wait_obs(bridge)
        assert len(obs) == 1 and obs[0].item_id == "aruco:7"
    finally:
        bridge.stop()


def test_bridge_rejects_wrong_or_missing_auth():
    bridge = NetworkBridgeSensor("bridge", host="127.0.0.1", port=0, auth_token="hunter2")
    bridge.start()
    try:
        bridge_send(bridge.port, [json.dumps({"auth": "wrong"}), POS_MSG])
        bridge_send(bridge.port, [POS_MSG])  # data before auth: also rejected
        time.sleep(0.3)
        assert bridge.poll() == []
        assert bridge.rejected_connections == 2
    finally:
        bridge.stop()


def test_bridge_without_token_unchanged():
    bridge = NetworkBridgeSensor("bridge", host="127.0.0.1", port=0)
    bridge.start()
    try:
        bridge_send(bridge.port, [POS_MSG])
        assert len(wait_obs(bridge)) == 1
    finally:
        bridge.stop()


def test_bridge_caps_line_length():
    bridge = NetworkBridgeSensor("bridge", host="127.0.0.1", port=0)
    bridge.start()
    try:
        huge = '{"type": "position", "item": "aruco:7", "pos": [1, 1, 1], "pad": "' \
            + "x" * (70 * 1024) + '"}'
        bridge_send(bridge.port, [huge, POS_MSG])
        obs = wait_obs(bridge)
        # the valid message still lands; the oversized one was dropped
        assert any(o.item_id == "aruco:7" for o in obs)
        assert bridge.dropped >= 1
    finally:
        bridge.stop()
