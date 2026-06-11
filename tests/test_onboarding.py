"""MCU onboarding: passive profiling + active echo benchmark."""

import json
import socket
import time

from hometwin.onboarding import PING_COUNT, DeviceProfile
from hometwin.sensors.network import NetworkBridgeSensor


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_passive_profile_rate_jitter_radio():
    clock = FakeClock()
    prof = DeviceProfile("esp32-hall", clock=clock)
    for i in range(50):
        clock.now += 0.1  # steady 10 Hz
        prof.note_message({"type": "rssi", "mac": "aa:bb", "rssi": -60.0 - (i % 5)})
    d = prof.as_dict()
    assert d["rate_hz"] == 10.0
    assert d["jitter_ms"] == 0.0
    radio = d["radio"]["AA:BB"]
    assert radio["count"] == 50
    assert -65 <= radio["mean"] <= -60
    assert radio["min"] == -64.0 and radio["max"] == -60.0
    assert radio["std"] > 0


def test_echo_benchmark_rtt_and_loss():
    clock = FakeClock()
    prof = DeviceProfile("node", clock=clock)
    pings = prof.start_benchmark({"type": "hello", "chip": "ESP32-D0WDQ6"})
    assert len(pings) == PING_COUNT
    assert prof.caps["chip"] == "ESP32-D0WDQ6"
    # answer 15 of 20 pings, 12 ms RTT
    for seq in range(15):
        clock.now += 0.012
        prof.note_pong(seq)
    clock.now += 20.0  # past the timeout: benchmark completes with loss
    echo = prof.echo_stats()
    assert echo["received"] == 15 and echo["loss"] == 0.25
    assert echo["complete"] is True
    assert echo["rtt_ms_min"] >= 12.0


def test_bridge_hello_triggers_pings_over_socket():
    bridge = NetworkBridgeSensor("bridge", host="127.0.0.1", port=0)
    bridge.start()
    try:
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=5) as sock:
            sock.sendall(json.dumps(
                {"type": "hello", "sensor_id": "esp32-hall", "chip": "ESP32"}).encode() + b"\n")
            sock.settimeout(5)
            buf = b""
            while buf.count(b"\n") < PING_COUNT:
                buf += sock.recv(4096)
            seqs = [json.loads(line)["seq"] for line in buf.decode().strip().split("\n")]
            assert seqs == list(range(PING_COUNT))
            # answer every ping like the firmware does
            for seq in seqs:
                sock.sendall(json.dumps(
                    {"type": "pong", "sensor_id": "esp32-hall", "seq": seq}).encode() + b"\n")
            deadline = time.time() + 5
            while time.time() < deadline:
                prof = bridge.profiles.get("esp32-hall")
                if prof and len(prof.rtts_ms) == PING_COUNT:
                    break
                time.sleep(0.02)
        prof = bridge.profiles["esp32-hall"].as_dict()
        assert prof["caps"]["chip"] == "ESP32"
        assert prof["echo"]["received"] == PING_COUNT
        assert prof["echo"]["loss"] == 0.0
        assert prof["echo"]["rtt_ms_mean"] >= 0
    finally:
        bridge.stop()


def test_profiles_in_overlay():
    from hometwin.overlay import map_overlay
    from hometwin.simulate import build_simulation

    tracker, state = build_simulation(seed=1)
    bridge = NetworkBridgeSensor("bridge", port=0)
    bridge.handle_line(json.dumps({"type": "hello", "sensor_id": "node-1", "fw": "x"}))
    bridge.handle_line(json.dumps(
        {"type": "rssi", "sensor_id": "node-1", "mac": "AA:BB:CC:DD:EE:FF",
         "anchor": [0, 0, 2], "rssi": -61}))
    tracker.sensors.append(bridge)
    for _ in range(3):
        state.tick()
        tracker.step()
    d = map_overlay(tracker)
    node = next(p for p in d["devices"] if p["sensor_id"] == "node-1")
    assert node["caps"]["fw"] == "x"
    assert "AA:BB:CC:DD:EE:FF" in node["radio"]
