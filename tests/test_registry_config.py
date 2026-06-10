import pytest

from apartment_tracker import registry
from apartment_tracker.config import load_config


def test_builtin_plugins_register():
    registry.load_plugins()
    sensors = registry.available("sensor")
    for name in ("camera", "ble_scanner", "rf_tomography", "network_bridge", "scripted"):
        assert name in sensors
    assert "aruco" in registry.available("detector")
    assert "onnx" in registry.available("detector")
    assert "opencv" in registry.available("frame_source")


def test_unknown_plugin_is_clear_error():
    registry.load_plugins()
    with pytest.raises(KeyError, match="no sensor plugin named 'warp_drive'"):
        registry.create("sensor", "warp_drive")
    with pytest.raises(ValueError):
        registry.register("spaceship", "x")


def test_custom_plugin_roundtrip():
    @registry.register("sensor", "test_custom_sensor")
    class Custom:
        def __init__(self, sensor_id="c", value=0):
            self.sensor_id, self.value = sensor_id, value

    built = registry.create("sensor", "test_custom_sensor", value=42)
    assert built.value == 42


def test_load_config_builds_world_items_sensors(tmp_path):
    cfg_file = tmp_path / "apartment.yaml"
    cfg_file.write_text(
        """
world:
  zones:
    - {name: kitchen, min: [5, 0, 0], max: [8, 4, 2.6]}
items:
  - id: keys
    name: House keys
    tags: ["aruco:7"]
sensors:
  - type: network_bridge
    id: bridge
    port: 0
tracker:
  poll_hz: 5
api:
  port: 9999
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.world.locate((6, 1, 1)) == "kitchen"
    assert cfg.items.resolve_tag("aruco:7") == "keys"
    assert len(cfg.sensors) == 1 and cfg.sensors[0].sensor_id == "bridge"
    assert cfg.poll_hz == 5.0
    assert cfg.api_port == 9999
