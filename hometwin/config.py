"""Config loading: one YAML file describes the apartment, the items, and the
sensor fleet. See configs/apartment.example.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hometwin import registry
from hometwin.items import ItemRegistry
from hometwin.sensors.base import SensorAdapter
from hometwin.world import World


@dataclass
class AppConfig:
    world: World
    items: ItemRegistry
    sensors: list[SensorAdapter]
    poll_hz: float = 10.0
    stale_after_s: float = 300.0
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    api_tokens: list[dict] = field(default_factory=list)  # [{token, role}]
    dataset_dir: str = "dataset"
    state_path: str | None = None
    save_interval_s: float = 30.0
    parallel_polling: bool = True
    presence_labels: frozenset = frozenset({"person"})
    anchors: dict = field(default_factory=dict)  # fiducial tag -> world position
    movables: object = None  # MovableRegistry (doors/drawers) or None
    motion_zones: object = None  # MotionZoneController or None
    hass: object = None  # HomeAssistantBridge or None
    device_tags: object = None  # DeviceTagSolver (tags on sensors) or None
    splat_asset: str | None = None  # .splat/.ply scan rendered by the dashboard's 3D tab
    floorplan: dict | None = None  # {image, width_m}: map-view background
    splat_transform: dict | None = None  # aligns the scan to the world frame
    config_path: str | None = None
    overrides_path: str | None = None  # dashboard-written settings (JSON)
    raw: dict = field(default_factory=dict)


def resolve_tokens(auth_cfg: dict | None) -> list[dict]:
    """Token specs -> [{token, role}]. A spec provides the secret as a
    literal `token`, an `env` var name, or a `file` path (0600 recommended).
    """
    out = []
    for spec in (auth_cfg or {}).get("tokens", []):
        if "token" in spec:
            secret = str(spec["token"])
        elif "env" in spec:
            secret = os.environ.get(spec["env"], "")
            if not secret:
                raise ValueError(f"auth token env var {spec['env']!r} is empty or unset")
        elif "file" in spec:
            secret = Path(spec["file"]).read_text(encoding="utf-8").strip()
        else:
            raise ValueError(f"auth token spec needs token/env/file: {spec}")
        role = spec.get("role", "admin")
        if role not in ("viewer", "admin"):
            raise ValueError(f"unknown auth role {role!r}")
        out.append({"token": secret, "role": role})
    return out


def build_sensor(cfg: dict) -> tuple[SensorAdapter, dict]:
    cfg = dict(cfg)
    kind = cfg.pop("type")
    sensor_id = cfg.pop("id", kind)
    device_tag = cfg.pop("device_tag", None)
    device_tag_correct = bool(cfg.pop("device_tag_correct", False))
    sensor = registry.create("sensor", kind, sensor_id=sensor_id, **cfg)
    return sensor, {"tag": device_tag, "correct": device_tag_correct}


def load_config(path: str | Path) -> AppConfig:
    registry.load_plugins()
    path = Path(path)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # dashboard-written settings live in a sidecar, so the hand-edited YAML
    # (and its comments) is never rewritten by the running app
    overrides_path = path.with_name(path.name + ".overrides.json")
    overrides = {}
    if overrides_path.exists():
        import json

        try:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8")) or {}
        except ValueError:
            overrides = {}
    sensor_overrides = overrides.get("sensors", {})
    sensor_cfgs = []
    for c in raw.get("sensors", []):
        c = dict(c)
        ov = sensor_overrides.get(str(c.get("id", c.get("type"))), {})
        if "source" in ov and isinstance(c.get("source"), dict):
            c["source"] = {**c["source"], **ov["source"]}
        sensor_cfgs.append(c)

    world_cfg = raw.get("world", {})
    world = World.from_config(world_cfg.get("zones", []), world_cfg.get("spots", []))
    items = ItemRegistry.from_config(raw.get("items", []), raw.get("item_sets", []))
    built = [build_sensor(c) for c in sensor_cfgs]
    sensors = [s for s, _ in built]

    device_tags = None
    tagged = [(s, m) for s, m in built if m["tag"] and hasattr(s, "position")]
    if tagged:
        from hometwin.devicetags import DeviceTagSolver

        device_tags = DeviceTagSolver()
        for s, m in tagged:
            device_tags.register(m["tag"], s, auto_correct=m["correct"])

    # anchors map tag -> candidate positions (twin strips carry two marker
    # centers under one ID; the calibrator matches the nearest hypothesis)
    anchors: dict[str, list[tuple]] = {}
    for a in world_cfg.get("anchors", []):
        positions = a.get("positions") or [a["position"]]
        anchors.setdefault(str(a["tag"]), []).extend(tuple(p) for p in positions)
    # a tagged spot is a surveyed fixed point: it calibrates cameras for free
    for spot in world.spots:
        if spot.tag and str(spot.tag) not in anchors:
            anchors[str(spot.tag)] = [tuple(p) for p in (spot.tag_positions or [spot.position])]

    movables = None
    if raw.get("world", {}).get("movables"):
        from hometwin.movables import MovableRegistry

        movables = MovableRegistry.from_config(world_cfg["movables"])
        for tag in movables.by_tag:
            if anchors.pop(tag, None) is not None:
                # a moving surface is never a calibration reference
                import logging

                logging.getLogger("hometwin").warning(
                    "tag %s is on a movable; removed from anchors", tag
                )

    motion_zones = None
    hass_bridge = None
    if raw.get("motion_zones") or raw.get("home_assistant"):
        from hometwin.hass import (
            HomeAssistantBridge, MiniMqtt, MotionZoneController,
        )

        ha_cfg = raw.get("home_assistant", {}) or {}
        motion_zones = MotionZoneController.from_config(
            raw.get("motion_zones", []),
            default_off_delay=float(ha_cfg.get("off_delay_s", 30.0)),
        )
        mqtt_cfg = ha_cfg.get("mqtt")
        if mqtt_cfg:
            hass_bridge = HomeAssistantBridge(
                MiniMqtt(
                    mqtt_cfg["host"],
                    int(mqtt_cfg.get("port", 1883)),
                    mqtt_cfg.get("username"),
                    mqtt_cfg.get("password"),
                ),
                discovery_prefix=ha_cfg.get("discovery_prefix", "homeassistant"),
            )

    for sensor in sensors:
        if anchors and hasattr(sensor, "attach_anchors"):
            sensor.attach_anchors(anchors)
        if movables is not None and hasattr(sensor, "attach_movables"):
            sensor.attach_movables(movables)
        if device_tags is not None and hasattr(sensor, "attach_device_tags"):
            sensor.attach_device_tags(device_tags)

    tracker = raw.get("tracker", {})
    api = raw.get("api", {})
    return AppConfig(
        world=world,
        items=items,
        sensors=sensors,
        poll_hz=float(tracker.get("poll_hz", 10.0)),
        stale_after_s=float(tracker.get("stale_after_s", 300.0)),
        api_host=api.get("host", "127.0.0.1"),
        api_port=int(api.get("port", 8080)),
        api_tokens=resolve_tokens(api.get("auth")),
        dataset_dir=tracker.get("dataset_dir", "dataset"),
        state_path=tracker.get("state_path"),
        save_interval_s=float(tracker.get("save_interval_s", 30.0)),
        parallel_polling=bool(tracker.get("parallel_polling", True)),
        presence_labels=frozenset(tracker.get("presence_labels", ["person"])),
        anchors=anchors,
        movables=movables,
        motion_zones=motion_zones,
        hass=hass_bridge,
        device_tags=device_tags,
        floorplan=raw.get("world", {}).get("floorplan"),
        splat_asset=raw.get("world", {}).get("splat_asset"),
        splat_transform=raw.get("world", {}).get("splat_transform"),
        config_path=str(path),
        overrides_path=str(overrides_path),
        raw=raw,
    )
