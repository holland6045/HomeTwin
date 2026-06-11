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


def test_dashboard_svg_renders_chrome_and_panels():
    from hometwin.render import dashboard_svg

    tracker, state = build_simulation(seed=1)
    for _ in range(70):
        state.tick()
        tracker.step()
    d = map_overlay(tracker)
    d["items"] = tracker.snapshot()
    svg = dashboard_svg(d)
    for needle in ("HomeTwin", "📷 cam-kitchen", "🧊 3D", "ITEMS",
                   "DOORS / DRAWERS", "utensil-drawer", "EVENTS", "Doors/drawers"):
        assert needle in svg, needle
