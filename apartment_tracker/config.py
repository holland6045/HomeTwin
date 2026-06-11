"""Config loading: one YAML file describes the apartment, the items, and the
sensor fleet. See configs/apartment.example.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from apartment_tracker import registry
from apartment_tracker.items import ItemRegistry
from apartment_tracker.sensors.base import SensorAdapter
from apartment_tracker.world import World


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
    anchors: dict = field(default_factory=dict)  # fiducial tag -> world position
    splat_asset: str | None = None  # .splat/.ply scan rendered by the dashboard's 3D tab
    splat_transform: dict | None = None  # aligns the scan to the world frame
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


def build_sensor(cfg: dict) -> SensorAdapter:
    cfg = dict(cfg)
    kind = cfg.pop("type")
    sensor_id = cfg.pop("id", kind)
    return registry.create("sensor", kind, sensor_id=sensor_id, **cfg)


def load_config(path: str | Path) -> AppConfig:
    registry.load_plugins()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    world_cfg = raw.get("world", {})
    world = World.from_config(world_cfg.get("zones", []), world_cfg.get("spots", []))
    items = ItemRegistry.from_config(raw.get("items", []), raw.get("item_sets", []))
    sensors = [build_sensor(c) for c in raw.get("sensors", [])]

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
    for sensor in sensors:
        if anchors and hasattr(sensor, "attach_anchors"):
            sensor.attach_anchors(anchors)

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
        anchors=anchors,
        splat_asset=raw.get("world", {}).get("splat_asset"),
        splat_transform=raw.get("world", {}).get("splat_transform"),
        raw=raw,
    )
