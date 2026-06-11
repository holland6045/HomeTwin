from hometwin.overlay import map_overlay
from hometwin.render import map_svg
from hometwin.simulate import build_simulation


def test_map_svg_contains_all_layers():
    tracker, state = build_simulation(seed=1)
    for _ in range(60):
        state.tick()
        tracker.step()
    svg = map_svg(map_overlay(tracker))
    assert svg.startswith("<svg")
    for needle in (
        "kitchen_counter",      # zone label
        "House keys",           # item label
        "counter-tray",         # spot
        "aruco:100",            # anchor tag
        "cam-living-a",         # camera label
        "presence",             # tomography blob
        "stroke-dasharray",     # bearing rays
        "polyline",             # trails
    ):
        assert needle in svg, needle


def test_map_svg_empty_overlay():
    svg = map_svg({"zones": [], "items": []})
    assert svg.startswith("<svg") and svg.endswith("</svg>")
