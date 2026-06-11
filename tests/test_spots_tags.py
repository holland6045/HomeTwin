"""Spots (drawer/shelf precision), item sets (identical boxes), styled tags."""

import pytest

from hometwin.config import load_config
from hometwin.fusion import FusionEngine
from hometwin.items import Item, ItemRegistry
from hometwin.observations import PositionObservation
from hometwin.simulate import build_simulation
from hometwin.tags import PALETTES, tag_svg
from hometwin.world import Spot, World, Zone


# --- spots ---------------------------------------------------------------------

def make_world():
    return World(
        [Zone("office", (0, 0, 0), (4, 4, 2.6))],
        [
            Spot("desk-drawer-2", (1.0, 1.0, 0.6), radius=0.25, tag="aruco:13"),
            Spot("desk-surface", (1.0, 1.0, 0.75), radius=0.8),
            Spot("shelf-b3", (3.5, 0.2, 1.6), radius=0.3),
        ],
    )


def test_spot_resolution_prefers_tightest():
    w = make_world()
    assert w.locate_spot((1.05, 1.0, 0.6)) == "desk-drawer-2"
    assert w.locate_spot((1.5, 1.3, 0.75)) == "desk-surface"
    assert w.locate_spot((3.5, 0.2, 1.55)) == "shelf-b3"
    assert w.locate_spot((2.5, 2.5, 1.0)) is None
    # zones still resolve independently
    assert w.locate((1.0, 1.0, 0.6)) == "office"


def test_snapshot_reports_spot():
    items = ItemRegistry()
    items.add(Item("keys", "Keys", tag_ids=["aruco:7"]))
    eng = FusionEngine(items, make_world())
    eng.ingest(PositionObservation(
        sensor_id="cam", timestamp=10.0, item_id="aruco:7",
        position=(1.0, 1.0, 0.6), sigma_m=0.05,
    ))
    snap = eng.snapshot(now=11.0)[0]
    assert snap["spot"] == "desk-drawer-2"
    assert snap["zone"] == "office"


# --- item sets: identical storage boxes ----------------------------------------

def test_item_set_expansion():
    reg = ItemRegistry.from_config(
        [],
        [{"id_prefix": "box-", "count": 12, "first_tag_id": 20, "labels": ["storage box"]}],
    )
    ids = [i.item_id for i in reg.all()]
    assert ids[0] == "box-01" and ids[-1] == "box-12"
    assert reg.resolve_tag("aruco:20") == "box-01"
    assert reg.resolve_tag("aruco:31") == "box-12"
    assert reg.get("box-03").name == "Box 03"
    # shared label is ambiguous by design: no single resolution
    assert reg.resolve_label("storage box") is None
    assert len(reg.label_candidates("storage box")) == 12


def test_ambiguous_label_associates_to_nearest_track_only():
    reg = ItemRegistry.from_config(
        [], [{"id_prefix": "box-", "count": 3, "first_tag_id": 20, "labels": ["storage box"]}]
    )
    eng = FusionEngine(reg, make_world())
    # box-01 was identified by its tag on the shelf; box-02 in the drawer
    for i in range(5):
        eng.ingest(PositionObservation(
            sensor_id="cam", timestamp=1.0 + i, item_id="aruco:20",
            position=(3.5, 0.2, 1.6), sigma_m=0.05,
        ))
        eng.ingest(PositionObservation(
            sensor_id="cam", timestamp=1.0 + i, item_id="aruco:21",
            position=(1.0, 1.0, 0.6), sigma_m=0.05,
        ))
    # anonymous "storage box" sighting near the shelf refines box-01
    applied = eng.ingest(PositionObservation(
        sensor_id="cam2", timestamp=7.0, label="storage box",
        position=(3.45, 0.22, 1.6), sigma_m=0.05,
    ))
    assert applied == "box-01"
    # an anonymous box in the middle of nowhere matches no track: ignored,
    # and it must never seed a track for an arbitrary candidate
    assert eng.ingest(PositionObservation(
        sensor_id="cam2", timestamp=7.5, label="storage box",
        position=(2.5, 2.9, 0.2), sigma_m=0.05,
    )) is None
    assert "box-03" not in eng.tracks


def test_spot_transition_event():
    from hometwin.config import AppConfig
    from hometwin.sensors.mock import ScriptedSensor
    from hometwin.tracker import Tracker

    items = ItemRegistry()
    items.add(Item("keys", "Keys", tag_ids=["aruco:7"]))

    def obs(pos, ts):
        return PositionObservation(
            sensor_id="cam", timestamp=ts, item_id="aruco:7", position=pos, sigma_m=0.03
        )

    batches = [[obs((3.5, 0.2, 1.6), 10.0 + i)] for i in range(4)]  # shelf-b3
    batches += [[obs((1.0, 1.0, 0.6), 20.0 + i)] for i in range(8)]  # drawer
    tracker = Tracker(AppConfig(world=make_world(), items=items,
                                sensors=[ScriptedSensor("cam", batches)]))
    for _ in range(len(batches)):
        tracker.step()
    moves = [e for e in tracker.events if e.get("to_spot") == "desk-drawer-2"]
    assert moves and moves[0]["from_spot"] == "shelf-b3"


# --- config plumbing -----------------------------------------------------------

def test_config_spots_sets_and_anchor_merge(tmp_path):
    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text(
        """
world:
  zones:
    - {name: office, min: [0, 0, 0], max: [4, 4, 2.6]}
  spots:
    - {name: desk-drawer-2, tag: "aruco:13", position: [1.0, 1.0, 0.6], radius: 0.2}
    - {name: shelf-b3, position: [3.5, 0.2, 1.6]}
  anchors:
    - {tag: "aruco:100", position: [0.1, 0.1, 1.4]}
item_sets:
  - id_prefix: box-
    count: 4
    first_tag_id: 20
    labels: [storage box]
items: []
sensors: []
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.world.locate_spot((1.0, 1.05, 0.6)) == "desk-drawer-2"
    # the tagged spot is a surveyed point: merged into the anchor map
    assert cfg.anchors == {
        "aruco:100": [(0.1, 0.1, 1.4)],
        "aruco:13": [(1.0, 1.0, 0.6)],
    }
    assert cfg.items.resolve_tag("aruco:22") == "box-03"


def test_simulation_keys_resolve_to_spot():
    tracker, state = build_simulation(seed=4)
    for _ in range(40):
        state.tick()
        tracker.step()
    snap = {e["item_id"]: e for e in tracker.engine.snapshot(now=state.now)}
    assert snap["keys"]["spot"] == "counter-tray"
    from hometwin.overlay import map_overlay

    d = map_overlay(tracker)
    assert d["spots"][0]["name"] == "counter-tray"


# --- styled tag labels ----------------------------------------------------------

def checker_bits(n=6):
    return [[(x + y) % 2 for x in range(n)] for y in range(n)]


def test_tag_svg_structure():
    bits = checker_bits()
    svg = tag_svg(bits, "BOX/07", caption="shelf b3", palette="cyan")
    assert svg.startswith("<svg")
    assert "BOX/07" in svg
    assert "SHELF B3" in svg
    assert PALETTES["cyan"] in svg
    # one black rect per set bit inside the marker field
    black = sum(sum(row) for row in bits)
    assert svg.count(f'fill="#0d0f12"/>') >= black


def test_tag_svg_marker_cells_exact():
    bits = [[0] * 6 for _ in range(6)]
    bits[2][3] = 1
    svg = tag_svg(bits, "T")
    # field 400 px, 1-cell quiet zone: cell = (400 - 2*50) / 6 = 50.00 px
    one_cell = [ln for ln in svg.splitlines() if 'width="50.00"' in ln]
    assert len(one_cell) == 1  # exactly the single set bit


def test_tag_svg_rejects_unknown_palette():
    with pytest.raises(ValueError, match="palette"):
        tag_svg(checker_bits(), "X", palette="vantablack")


def test_tag_svg_print_size():
    svg = tag_svg(checker_bits(), "X", size_mm=80.0)
    assert 'width="80.0mm"' in svg
    assert 'height="106.7mm"' in svg  # 3:4 plate


def test_wide_tag_layout():
    svg = tag_svg(checker_bits(), "SHF/B3", caption="shelf b3", palette="acid",
                  layout="wide", size_mm=200.0)
    assert 'width="200.0mm"' in svg
    assert 'height="56.0mm"' in svg  # 25:7 strip
    assert "SHF/B3" in svg and "SHELF B3 // HOMETWIN" in svg
    # marker keeps near-full plate height: 220 px field on a 280 px plate,
    # cell = (220 - 2*27.5) / 6 = 27.50
    assert svg.count('width="27.50"') == sum(sum(r) for r in checker_bits())


def test_wide_tag_twin_markers():
    bits = checker_bits()
    single = tag_svg(bits, "X", layout="wide")
    twin = tag_svg(bits, "X", layout="wide", twin=True)
    black = sum(sum(r) for r in bits)
    assert single.count('width="27.50"') == black
    assert twin.count('width="27.50"') == 2 * black  # marker at both ends
    # twin replaces the hazard strip at the right end
    assert "hzw" in single and "hzw" not in twin


def test_wide_tag_distinct_lr_codes():
    """L/R twin: a DIFFERENT marker id on the right end — each end becomes
    an ordinary unambiguous anchor."""
    left = checker_bits()
    right = [[1 - b for b in row] for row in left]  # inverted: distinct pattern
    svg = tag_svg(left, "X", layout="wide", twin_bits=right)
    total = sum(sum(r) for r in left) + sum(sum(r) for r in right)
    assert svg.count('width="27.50"') == total
    assert "hzw" not in svg  # twin replaces the hazard strip


def test_tag_svg_rejects_unknown_layout():
    with pytest.raises(ValueError, match="layout"):
        tag_svg(checker_bits(), "X", layout="circular")
