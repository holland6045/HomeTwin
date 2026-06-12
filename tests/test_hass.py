"""Home Assistant motion zones: state machine, MQTT wire protocol, wiring."""

import json
import socket
import struct
import threading

import pytest

from hometwin.hass import (
    HomeAssistantBridge, MiniMqtt, MotionZone, MotionZoneController, slugify,
)


# --- zone state machine ----------------------------------------------------------

def test_zone_geometry_and_hit():
    box = MotionZone("Living Room", min_corner=(0, 0, 0), max_corner=(5, 4, 2.6))
    assert box.hit((2, 2, 1))
    assert not box.hit((6, 2, 1))
    assert box.hit((5.2, 2, 1), sigma_m=0.5)  # sigma margin reaches in
    circle = MotionZone("hall", center=(8, 2), radius=1.0)
    assert circle.hit((8.5, 2.5, 1.7))   # z ignored for circles
    assert not circle.hit((10, 2, 0))
    assert slugify("Living Room!") == "living_room"
    with pytest.raises(ValueError):
        MotionZone("bad")


def test_on_off_delay_semantics():
    ctl = MotionZoneController([
        MotionZone("a", min_corner=(0, 0, 0), max_corner=(2, 2, 2), off_delay_s=10.0)])
    assert [z.name for z in ctl.evidence((1, 1, 1), ts=100.0)] == ["a"]
    assert ctl.evidence((1, 1, 1), ts=101.0) == []  # already on: no re-trigger
    assert ctl.expire(now=105.0) == []              # within delay
    assert [z.name for z in ctl.expire(now=111.5)] == ["a"]
    assert ctl.snapshot()[0]["on"] is False
    # fresh evidence re-arms
    assert [z.name for z in ctl.evidence((1, 1, 1), ts=120.0)] == ["a"]


def test_duplicate_zone_rejected():
    ctl = MotionZoneController()
    ctl.add(MotionZone("Desk Area", center=(1, 1), radius=1))
    with pytest.raises(ValueError):
        ctl.add(MotionZone("desk area!", center=(2, 2), radius=1))


# --- MQTT wire protocol against a real socket stub --------------------------------

class BrokerStub:
    """Accepts one client, answers CONNACK, records PUBLISH packets."""

    def __init__(self):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.published = []  # (topic, payload, retain)
        self.connect_packet = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _read_packet(self, sock, buf):
        while True:
            # need at least fixed header + remaining length
            i, mult, length = 1, 1, 0
            while True:
                if len(buf) <= i:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return None, buf
                    buf += chunk
                    continue
                byte = buf[i]
                length += (byte & 0x7F) * mult
                mult *= 128
                i += 1
                if not byte & 0x80:
                    break
            total = i + length
            while len(buf) < total:
                chunk = sock.recv(4096)
                if not chunk:
                    return None, buf
                buf += chunk
            return buf[:total], buf[total:]

    def _run(self):
        sock, _ = self.server.accept()
        buf = b""
        packet, buf = self._read_packet(sock, buf)
        self.connect_packet = packet
        sock.sendall(bytes([0x20, 0x02, 0x00, 0x00]))  # CONNACK accepted
        while True:
            packet, buf = self._read_packet(sock, buf)
            if packet is None:
                return
            ptype = packet[0] >> 4
            if ptype == 3:  # PUBLISH
                retain = bool(packet[0] & 0x01)
                # skip remaining-length varint
                i = 1
                while packet[i] & 0x80:
                    i += 1
                i += 1
                tlen = struct.unpack(">H", packet[i:i + 2])[0]
                topic = packet[i + 2:i + 2 + tlen].decode()
                payload = packet[i + 2 + tlen:].decode()
                self.published.append((topic, payload, retain))
            elif ptype == 14:  # DISCONNECT
                return


def test_mqtt_discovery_and_state_on_wire():
    broker = BrokerStub()
    bridge = HomeAssistantBridge(MiniMqtt("127.0.0.1", broker.port, "user", "pw"))
    zone = MotionZone("Living Room", min_corner=(0, 0, 0), max_corner=(5, 4, 2.6))
    bridge.announce(zone)
    zone.on = True
    bridge.publish_state(zone)
    bridge.close()
    broker._thread.join(timeout=5)

    # CONNECT carries protocol name MQTT and our credentials flags
    assert b"MQTT" in broker.connect_packet
    assert b"hometwin" in broker.connect_packet and b"user" in broker.connect_packet

    by_topic = {}
    for topic, payload, retain in broker.published:
        by_topic.setdefault(topic, []).append((payload, retain))
    cfg_topic = "homeassistant/binary_sensor/hometwin_living_room/config"
    payload, retain = by_topic[cfg_topic][0]
    assert retain is True
    cfg = json.loads(payload)
    assert cfg["device_class"] == "motion"
    assert cfg["unique_id"] == "hometwin_motion_living_room"
    assert cfg["device"]["identifiers"] == ["hometwin_zone_living_room"]
    assert by_topic["hometwin/availability"][0][0] == "online"
    states = [p for p, _ in by_topic["hometwin/motion/living_room/state"]]
    assert states[0] == "OFF" and states[-1] == "ON"
    assert by_topic["hometwin/availability"][-1][0] == "offline"


def test_mqtt_broker_down_never_raises():
    bridge = HomeAssistantBridge(MiniMqtt("127.0.0.1", 1))  # nothing listens
    zone = MotionZone("x", center=(0, 0), radius=1)
    bridge.publish_state(zone)  # absorbed
    assert zone.slug not in bridge._announced


# --- tracker integration -----------------------------------------------------------

def test_presence_drives_zone_and_overlay():
    from hometwin.overlay import map_overlay
    from hometwin.simulate import build_simulation

    tracker, state = build_simulation(seed=1)
    tracker.cfg.motion_zones = MotionZoneController([
        MotionZone("living-motion", min_corner=(0, 0, 0), max_corner=(5, 4, 2.6),
                   off_delay_s=30.0),
        MotionZone("kitchen-motion", min_corner=(6.2, 0, 0), max_corner=(8, 4, 2.6),
                   off_delay_s=30.0),
    ])
    for _ in range(20):
        state.tick()
        tracker.step()
    zones = {z["name"]: z for z in map_overlay(tracker)["motion_zones"]}
    assert zones["living-motion"]["on"] is True   # the person paces here
    assert zones["kitchen-motion"]["on"] is False
    assert any(e.get("motion_zone") == "living-motion" and e["state"] == "ON"
               for e in tracker.events)


def test_zone_turns_off_after_quiet_period():
    from hometwin.config import AppConfig
    from hometwin.items import ItemRegistry
    from hometwin.observations import AreaObservation
    from hometwin.sensors.mock import ScriptedSensor
    from hometwin.tracker import Tracker
    from hometwin.world import World, Zone

    sensor = ScriptedSensor("rti")
    cfg = AppConfig(world=World([Zone("r", (0, 0, 0), (5, 5, 3))]),
                    items=ItemRegistry(), sensors=[sensor],
                    motion_zones=MotionZoneController([
                        MotionZone("z", center=(2, 2), radius=1, off_delay_s=20.0)]))
    tracker = Tracker(cfg)
    sensor.push([AreaObservation(sensor_id="rti", timestamp=100.0, label="presence",
                                 centroid=(2, 2, 1), sigma_m=0.5)])
    tracker.step()
    assert tracker.cfg.motion_zones.snapshot()[0]["on"] is True
    # quiet evidence elsewhere advances the motion clock past the delay
    sensor.push([AreaObservation(sensor_id="rti", timestamp=125.0, label="presence",
                                 centroid=(4.8, 4.8, 1), sigma_m=0.1)])
    tracker.step()
    assert tracker.cfg.motion_zones.snapshot()[0]["on"] is False
    assert any(e.get("motion_zone") == "z" and e["state"] == "OFF"
               for e in tracker.events)


def test_runtime_zone_creation_endpoint():
    import urllib.request

    from hometwin.api import ApiServer
    from hometwin.simulate import build_simulation

    tracker, _ = build_simulation(seed=1)
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{api.port}/motion-zones",
            data=json.dumps({"name": "desk watch", "center": [1.5, 1.0],
                             "radius": 0.8}).encode(),
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["slug"] == "desk_watch"
        names = [z["name"] for z in tracker.cfg.motion_zones.snapshot()]
        assert names == ["desk watch"]
    finally:
        api.stop()
