import json
import math
import socket
import time

import pytest

from apartment_tracker.observations import Detection, PositionObservation, RangeObservation
from apartment_tracker.sensors.ble import BLEScannerSensor, PathLossModel, RSSISource
from apartment_tracker.sensors.camera import CameraGeometry, CameraSensor, StaticFrameSource
from apartment_tracker.sensors.network import NetworkBridgeSensor
from apartment_tracker.sensors.tomography import RTIGrid, TomographySensor, LinkSource


# --- camera geometry ---------------------------------------------------------

def test_straight_down_camera_centers_on_position():
    geo = CameraGeometry(position=(2.0, 3.0, 2.5), yaw_deg=0, pitch_deg=90)
    p = geo.project_to_plane(0.5, 0.5, 0.0)
    assert p == pytest.approx((2.0, 3.0, 0.0), abs=1e-6)


def test_ray_above_horizon_misses_floor():
    geo = CameraGeometry(position=(0, 0, 2.0), yaw_deg=0, pitch_deg=0)
    assert geo.project_to_plane(0.5, 0.2, 0.0) is None  # upper half of image


def test_tilted_camera_hits_plane_in_front():
    geo = CameraGeometry(position=(0.0, 0.0, 2.0), yaw_deg=0, pitch_deg=45)
    p = geo.project_to_plane(0.5, 0.5, 0.0)
    assert p is not None
    assert p[0] == pytest.approx(2.0, abs=1e-6)  # 45 degrees down from 2 m
    assert p[1] == pytest.approx(0.0, abs=1e-6)


class OneShotDetector:
    def detect(self, frame):
        return [Detection(label="keys", confidence=0.9, bbox=(0.5, 0.5, 0.0, 0.0), tag_id="aruco:7")]


def test_camera_sensor_emits_position_observation():
    geo = CameraGeometry(position=(1.0, 1.0, 2.0), yaw_deg=0, pitch_deg=90)
    cam = CameraSensor(
        "cam", geometry=geo, frame_source=StaticFrameSource([object()]),
        detector=OneShotDetector(), surface_z=0.0,
    )
    obs = cam.poll()
    assert len(obs) == 1
    assert isinstance(obs[0], PositionObservation)
    assert obs[0].item_id == "aruco:7"
    assert obs[0].position == pytest.approx((1.0, 1.0, 0.0), abs=1e-6)
    assert cam.poll() == []  # source exhausted


# --- BLE ---------------------------------------------------------------------

def test_path_loss_roundtrip():
    m = PathLossModel(tx_power=-59.0, exponent=2.7)
    for d in (0.5, 1.0, 3.0, 10.0):
        assert m.rssi_to_range(m.range_to_rssi(d)) == pytest.approx(d, rel=1e-9)


class FixedRSSI(RSSISource):
    def __init__(self, readings):
        self._r = readings

    def readings(self):
        return self._r


def test_ble_scanner_emits_tagged_range():
    s = BLEScannerSensor("ble-1", position=(0, 0, 2.2))
    s.source = FixedRSSI([("aa:11:22:33:44:55", s.model.range_to_rssi(2.0))])
    obs = s.poll()
    assert len(obs) == 1
    assert isinstance(obs[0], RangeObservation)
    assert obs[0].item_id == "ble:AA:11:22:33:44:55"
    assert obs[0].range_m == pytest.approx(2.0, rel=1e-6)


# --- tomography --------------------------------------------------------------

def square_mesh():
    return {
        "n0": (0.0, 0.0), "n1": (2.0, 0.0), "n2": (4.0, 0.0),
        "n3": (4.0, 2.0), "n4": (4.0, 4.0), "n5": (2.0, 4.0),
        "n6": (0.0, 4.0), "n7": (0.0, 2.0),
    }


def synth_readings(nodes, person, lam=0.35, base=-50.0, atten=8.0):
    names = sorted(nodes)
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ax, ay = nodes[a]
            bx, by = nodes[b]
            link = math.hypot(bx - ax, by - ay)
            rss = base
            if person is not None:
                px, py = person
                excess = math.hypot(px - ax, py - ay) + math.hypot(px - bx, py - by) - link
                if excess < lam:
                    rss -= atten
            out.append((a, b, rss))
    return out


def test_rti_reconstruction_localizes_target():
    nodes = square_mesh()
    grid = RTIGrid(nodes, (0, 0, 4, 4), cell_m=0.25)
    person = (1.0, 2.5)
    atten = {}
    for a, b, rss in synth_readings(nodes, person):
        if -50.0 - rss >= 2.0:
            atten[(a, b) if a <= b else (b, a)] = -50.0 - rss
    blob = grid.blob(grid.reconstruct(atten))
    assert blob is not None
    (cx, cy), spread, _ = blob
    assert math.hypot(cx - person[0], cy - person[1]) < 0.6


class SynthLinks(LinkSource):
    def __init__(self, nodes, person):
        self.nodes, self.person = nodes, person

    def readings(self):
        return synth_readings(self.nodes, self.person)


def test_tomography_sensor_emits_area_observation():
    nodes = square_mesh()
    sensor = TomographySensor("rti", nodes=nodes, bounds=(0, 0, 4, 4))
    sensor.calibrate(synth_readings(nodes, None))
    sensor.source = SynthLinks(nodes, (3.0, 1.0))
    obs = sensor.poll()
    assert len(obs) == 1
    assert obs[0].label == "presence"
    assert math.hypot(obs[0].centroid[0] - 3.0, obs[0].centroid[1] - 1.0) < 0.6


def test_tomography_empty_room_silent():
    nodes = square_mesh()
    sensor = TomographySensor("rti", nodes=nodes, bounds=(0, 0, 4, 4))
    sensor.calibrate(synth_readings(nodes, None))
    sensor.source = SynthLinks(nodes, None)
    assert sensor.poll() == []


# --- network bridge ----------------------------------------------------------

def test_bridge_parses_all_message_types():
    bridge = NetworkBridgeSensor("bridge", port=0)
    bridge.handle_line(json.dumps(
        {"type": "position", "item": "aruco:7", "pos": [1, 2, 0.5], "sigma_m": 0.2}
    ))
    bridge.handle_line(json.dumps(
        {"type": "range", "item": "ble:AA", "anchor": [0, 0, 2], "range_m": 3.0}
    ))
    bridge.handle_line(json.dumps(
        {"type": "rssi", "mac": "aa:bb:cc:dd:ee:ff", "anchor": [0, 0, 2], "rssi": -59.0}
    ))
    bridge.handle_line(json.dumps(
        {"type": "area", "label": "presence", "centroid": [2, 2, 1]}
    ))
    bridge.handle_line("not json at all")
    bridge.handle_line(json.dumps({"type": "bogus"}))
    obs = bridge.poll()
    assert len(obs) == 4
    assert bridge.dropped == 2
    rssi_obs = obs[2]
    assert rssi_obs.item_id == "ble:AA:BB:CC:DD:EE:FF"
    assert rssi_obs.range_m == pytest.approx(1.0, rel=1e-6)  # -59 dBm at tx_power -59
    assert bridge.poll() == []


def test_bridge_over_real_socket():
    bridge = NetworkBridgeSensor("bridge", host="127.0.0.1", port=0)
    bridge.start()
    try:
        with socket.create_connection(("127.0.0.1", bridge.port), timeout=5) as sock:
            sock.sendall(
                (json.dumps({"type": "position", "item": "aruco:7", "pos": [1, 1, 1]}) + "\n").encode()
            )
        deadline = time.time() + 5
        obs = []
        while not obs and time.time() < deadline:
            obs = bridge.poll()
            time.sleep(0.01)
        assert len(obs) == 1
        assert obs[0].item_id == "aruco:7"
    finally:
        bridge.stop()
