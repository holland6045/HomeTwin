"""Config loading: one YAML file describes the apartment, the items, and the
sensor fleet. See configs/apartment.example.yaml.
"""

from __future__ import annotations

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
    dataset_dir: str = "dataset"
    state_path: str | None = None
    save_interval_s: float = 30.0
    splat_asset: str | None = None  # .splat/.ply scan rendered by the dashboard's 3D tab
    splat_transform: dict | None = None  # aligns the scan to the world frame
    raw: dict = field(default_factory=dict)


def build_sensor(cfg: dict) -> SensorAdapter:
    cfg = dict(cfg)
    kind = cfg.pop("type")
    sensor_id = cfg.pop("id", kind)
    return registry.create("sensor", kind, sensor_id=sensor_id, **cfg)


def load_config(path: str | Path) -> AppConfig:
    registry.load_plugins()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    world = World.from_config(raw.get("world", {}).get("zones", []))
    items = ItemRegistry.from_config(raw.get("items", []))
    sensors = [build_sensor(c) for c in raw.get("sensors", [])]

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
        dataset_dir=tracker.get("dataset_dir", "dataset"),
        state_path=tracker.get("state_path"),
        save_interval_s=float(tracker.get("save_interval_s", 30.0)),
        splat_asset=raw.get("world", {}).get("splat_asset"),
        splat_transform=raw.get("world", {}).get("splat_transform"),
        raw=raw,
    )
