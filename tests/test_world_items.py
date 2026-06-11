import pytest

from hometwin.items import Item, ItemRegistry
from hometwin.world import World, Zone


def make_world():
    return World(
        [
            Zone("apartment", (0, 0, 0), (8, 4, 2.6)),
            Zone("kitchen", (5, 0, 0), (8, 4, 2.6)),
            Zone("counter", (6, 0.5, 0.8), (7.5, 1.5, 1.1)),
        ]
    )


def test_locate_prefers_smallest_zone():
    w = make_world()
    assert w.locate((6.5, 1.0, 0.9)) == "counter"
    assert w.locate((5.5, 3.0, 1.0)) == "kitchen"
    assert w.locate((1.0, 1.0, 1.0)) == "apartment"
    assert w.locate((20.0, 20.0, 0.0)) is None


def test_world_from_config():
    w = World.from_config([{"name": "a", "min": [0, 0, 0], "max": [1, 1, 1]}])
    assert w.locate((0.5, 0.5, 0.5)) == "a"


def test_item_registry_resolution():
    reg = ItemRegistry()
    reg.add(Item("keys", "House keys", labels=["keys"], tag_ids=["aruco:7"]))
    assert reg.resolve_tag("aruco:7") == "keys"
    assert reg.resolve_label("keys") == "keys"
    assert reg.resolve_tag("aruco:9") is None


def test_runtime_tagging():
    reg = ItemRegistry()
    reg.add(Item("wallet", "Wallet"))
    reg.tag_item("wallet", "ble:AA:BB:CC:DD:EE:FF")
    assert reg.resolve_tag("ble:AA:BB:CC:DD:EE:FF") == "wallet"


def test_duplicate_item_rejected():
    reg = ItemRegistry()
    reg.add(Item("keys", "Keys"))
    with pytest.raises(ValueError):
        reg.add(Item("keys", "Other keys"))
