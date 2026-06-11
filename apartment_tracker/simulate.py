"""Synthetic apartment simulation.

Exercises the full pipeline with zero hardware: a simulated camera watching
the kitchen (ArUco-tagged keys), three BLE scanners trilaterating a wallet
and a carried phone, and an RF tomography mesh seeing the person carrying
the phone. Used by `apartment-tracker simulate` and the end-to-end test.
"""

from __future__ import annotations

import math
import random
import time

from apartment_tracker.config import AppConfig
from apartment_tracker.items import Item, ItemRegistry
from apartment_tracker.observations import Detection
from apartment_tracker.sensors.ble import BLEScannerSensor, RSSISource
from apartment_tracker.sensors.camera import CameraGeometry, CameraSensor
from apartment_tracker.sensors.tomography import LinkSource, TomographySensor
from apartment_tracker.tracker import Tracker
from apartment_tracker.world import World, Zone

WALLET_MAC = "AA:11:22:33:44:55"
PHONE_MAC = "BB:66:77:88:99:00"

TRUE_KEYS = (6.5, 1.0, 0.9)  # kitchen counter
TRUE_WALLET = (2.0, 3.2, 0.45)  # sofa
ANCHOR_TAG = "aruco:100"
ANCHOR_POS = (6.9, 1.5, 0.9)  # blocky calibration target on the counter
CAM_KITCHEN_YAW_TRUE = -110.0
CAM_KITCHEN_YAW_BUMPED = -108.5  # config is 1.5 deg off; the anchor heals it


def project_to_pixel(geo: CameraGeometry, p: tuple[float, float, float]):
    """World point to normalized pixel, only if inside the frame."""
    pix = geo.world_to_pixel(p)
    if pix is None or not (0.0 <= pix[0] <= 1.0 and 0.0 <= pix[1] <= 1.0):
        return None
    return pix


class SimWorldState:
    """Ground truth: static items plus a person carrying the phone.

    Keeps its own clock (sensors are pointed at it) so simulated motion and
    observation timestamps advance together regardless of wall-clock speed.
    """

    def __init__(self, seed: int = 1):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.now = time.time()
        self.person = [1.0, 1.0]

    def tick(self, dt: float = 0.5) -> None:
        self.t += dt
        self.now += dt
        # person paces the living room along x
        self.person[0] = 2.0 + 1.5 * math.sin(self.t / 8.0)
        self.person[1] = 2.0

    @property
    def phone(self) -> tuple[float, float, float]:
        return (self.person[0], self.person[1], 1.0)


class SimArucoDetector:
    """Reports the keys' tag and the calibration anchor as the *physical*
    camera would see them (true geometry, not the configured one)."""

    def __init__(self, geo: CameraGeometry, state: SimWorldState, noise_px: float = 0.004):
        self.geo, self.state, self.noise = geo, state, noise_px

    def detect(self, frame) -> list[Detection]:
        rng = self.state.rng
        out = []
        for tag, pos in (("aruco:7", TRUE_KEYS), (ANCHOR_TAG, ANCHOR_POS)):
            pix = project_to_pixel(self.geo, pos)
            if pix is None:
                continue
            u = pix[0] + rng.gauss(0, self.noise)
            v = pix[1] + rng.gauss(0, self.noise)
            out.append(Detection(label="aruco", confidence=1.0, bbox=(u, v, 0.0, 0.0), tag_id=tag))
        return out


class SimFrameSource:
    def get_frame(self):
        return object()  # opaque; the sim detector ignores it


class SimPhoneDetector:
    """Anonymous 'phone' label sightings — exercises ray-mode cameras and
    label-based association (no tag in view)."""

    def __init__(self, geo: CameraGeometry, state: SimWorldState, noise_px: float = 0.004):
        self.geo, self.state, self.noise = geo, state, noise_px

    def detect(self, frame) -> list[Detection]:
        pix = project_to_pixel(self.geo, self.state.phone)
        if pix is None:
            return []
        rng = self.state.rng
        u = pix[0] + rng.gauss(0, self.noise)
        v = pix[1] + rng.gauss(0, self.noise)
        return [Detection(label="phone", confidence=0.85, bbox=(u, v, 0.0, 0.0))]


class SimRSSISource(RSSISource):
    def __init__(self, scanner_pos: tuple, state: SimWorldState, model, noise_db: float = 2.0):
        self.pos, self.state, self.model, self.noise = scanner_pos, state, model, noise_db

    def readings(self) -> list[tuple[str, float]]:
        out = []
        for mac, p in ((WALLET_MAC, TRUE_WALLET), (PHONE_MAC, self.state.phone)):
            d = math.dist(self.pos, p)
            out.append((mac, self.model.range_to_rssi(d) + self.state.rng.gauss(0, self.noise)))
        return out


class SimLinkSource(LinkSource):
    """Synthesizes link RSS: baseline minus attenuation when the person sits
    inside the link's sensing ellipse."""

    def __init__(self, nodes: dict, state: SimWorldState, lam: float = 0.35):
        self.nodes, self.state, self.lam = nodes, state, lam
        self.names = sorted(nodes)

    def readings(self, with_person: bool = True) -> list[tuple[str, str, float]]:
        out = []
        px, py = self.state.person
        for i, a in enumerate(self.names):
            for b in self.names[i + 1 :]:
                ax, ay = self.nodes[a]
                bx, by = self.nodes[b]
                link = math.hypot(bx - ax, by - ay)
                rss = -50.0 + self.state.rng.gauss(0, 0.3)
                if with_person:
                    excess = (
                        math.hypot(px - ax, py - ay) + math.hypot(px - bx, py - by) - link
                    )
                    if excess < self.lam:
                        rss -= 8.0
                out.append((a, b, rss))
        return out


def build_simulation(seed: int = 1) -> tuple[Tracker, SimWorldState]:
    state = SimWorldState(seed)

    world = World(
        [
            Zone("living_room", (0, 0, 0), (5, 4, 2.6)),
            Zone("kitchen", (5, 0, 0), (8, 4, 2.6)),
            Zone("kitchen_counter", (6, 0.5, 0.8), (7.5, 1.5, 1.1)),
            Zone("sofa", (1.5, 2.8, 0.3), (3.0, 3.8, 0.7)),
        ]
    )
    items = ItemRegistry()
    items.add(Item("keys", "House keys", labels=["keys"], tag_ids=["aruco:7"]))
    items.add(Item("wallet", "Wallet", tag_ids=[f"ble:{WALLET_MAC}"]))
    items.add(Item("phone", "Phone", labels=["phone"], tag_ids=[f"ble:{PHONE_MAC}"]))

    # detectors see through the camera's TRUE mounting; the sensor is
    # configured with a bumped yaw that the anchor target corrects online
    true_geo = CameraGeometry(
        position=(7.5, 3.8, 2.3), yaw_deg=CAM_KITCHEN_YAW_TRUE, pitch_deg=40.0, hfov_deg=80.0
    )
    cfg_geo = CameraGeometry(
        position=(7.5, 3.8, 2.3), yaw_deg=CAM_KITCHEN_YAW_BUMPED, pitch_deg=40.0, hfov_deg=80.0
    )
    camera = CameraSensor(
        "cam-kitchen",
        geometry=cfg_geo,
        frame_source=SimFrameSource(),
        detector=SimArucoDetector(true_geo, state),
        surface_z=TRUE_KEYS[2],
        base_sigma_m=0.1,
        anchor_correct=True,
    )
    camera.attach_anchors({ANCHOR_TAG: ANCHOR_POS})

    # two overlapping living-room cameras in ray mode: their sight rays are
    # triangulated by the fusion engine — no surface assumption for the phone
    ray_cams = []
    for sid, pos, yaw in (
        ("cam-living-a", (0.0, 0.0, 2.4), 50.0),
        ("cam-living-b", (5.0, 4.0, 2.4), -130.0),
    ):
        rgeo = CameraGeometry(position=pos, yaw_deg=yaw, pitch_deg=25.0, hfov_deg=80.0)
        ray_cams.append(
            CameraSensor(
                sid,
                geometry=rgeo,
                frame_source=SimFrameSource(),
                detector=SimPhoneDetector(rgeo, state),
                mode="ray",
            )
        )

    scanners = []
    for sid, pos in (
        ("ble-entry", (0.0, 0.0, 2.2)),
        ("ble-bedroom", (0.0, 4.0, 2.2)),
        ("ble-kitchen", (8.0, 0.0, 2.2)),
    ):
        s = BLEScannerSensor(sid, position=pos)
        s.source = SimRSSISource(pos, state, s.model)
        scanners.append(s)

    rti_nodes = {
        "n0": (0.0, 0.0),
        "n1": (2.5, 0.0),
        "n2": (5.0, 0.0),
        "n3": (5.0, 2.0),
        "n4": (5.0, 4.0),
        "n5": (2.5, 4.0),
        "n6": (0.0, 4.0),
        "n7": (0.0, 2.0),
    }
    rti = TomographySensor(
        "rti-living",
        nodes=rti_nodes,
        bounds=(0.0, 0.0, 5.0, 4.0),
        cell_m=0.25,
    )
    link_source = SimLinkSource(rti_nodes, state)
    rti.calibrate(link_source.readings(with_person=False))
    rti.source = link_source

    cfg = AppConfig(
        world=world,
        items=items,
        sensors=[camera, *ray_cams, *scanners, rti],
        poll_hz=2.0,
        anchors={ANCHOR_TAG: ANCHOR_POS},
    )
    for sensor in cfg.sensors:
        sensor.clock = lambda: state.now
    return Tracker(cfg), state


def run_simulation(ticks: int = 120, seed: int = 1) -> dict:
    tracker, state = build_simulation(seed)
    for _ in range(ticks):
        state.tick()
        tracker.step()

    snapshot = {e["item_id"]: e for e in tracker.engine.snapshot(now=state.now)}
    truth = {"keys": TRUE_KEYS, "wallet": TRUE_WALLET, "phone": state.phone}
    errors = {}
    for item_id, true_pos in truth.items():
        est = snapshot[item_id].get("position")
        errors[item_id] = round(math.dist(est, true_pos), 3) if est else None
    return {"snapshot": snapshot, "errors_m": errors, "presence": tracker.presence}
