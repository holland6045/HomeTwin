"""Floorplan import: extraction scoring, cleanup, config/API plumbing."""

import json
import urllib.request

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from hometwin.floorplan import (
    clean_floorplan, find_floorplan_url, floorplan_meta, png_size,
)

LISTING = """
<html><body>
<img src="/static/logo.png" class="site-logo">
<img data-src="/media/hero-photo.jpg" alt="living room">
<img src="/media/units/b2/floorplan_b2.png" alt="B2 floor plan" class="fp-image">
<img src="/media/icons/map-pin.svg">
</body></html>
"""


def test_find_floorplan_url_scores_the_plan():
    url = find_floorplan_url(LISTING, "https://apts.example.com/unit/b2")
    assert url == "https://apts.example.com/media/units/b2/floorplan_b2.png"
    assert find_floorplan_url("<html><img src='x.png'></html>", "https://e.com") == "https://e.com/x.png"
    assert find_floorplan_url("<html>no images</html>", "https://e.com") is None


def synthetic_plan():
    """A floorplan-ish image: gray paper, dark walls, scan speckle."""
    img = np.full((400, 600), 215, dtype=np.uint8)
    cv2.rectangle(img, (60, 60), (540, 340), 40, 4)       # outer walls
    cv2.line(img, (300, 60), (300, 340), 40, 3)           # interior wall
    rng = np.random.default_rng(7)
    for _ in range(150):                                  # scan noise specks
        x, y = int(rng.integers(0, 600)), int(rng.integers(0, 400))
        img[y, x] = 0
    ok, jpg = cv2.imencode(".jpg", img)
    assert ok
    return jpg.tobytes()


def test_clean_floorplan_despeckles_and_crops(tmp_path):
    png = clean_floorplan(synthetic_plan())
    out = tmp_path / "plan.png"
    out.write_bytes(png)
    cleaned = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_GRAYSCALE)
    # cropped to wall content (+margin), not the original 600x400 canvas
    assert cleaned.shape[1] < 600 and cleaned.shape[0] < 400
    # walls survive, speckle is gone: dark pixels form few large components
    inverted = (cleaned < 128).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(inverted)
    assert all(stats[i, cv2.CC_STAT_AREA] >= 12 for i in range(1, n))
    assert inverted.sum() > 1000  # the walls themselves
    assert png_size(out) == (cleaned.shape[1], cleaned.shape[0])


def test_clean_rejects_garbage():
    with pytest.raises(ValueError):
        clean_floorplan(b"this is not an image")


def test_floorplan_meta_and_api(tmp_path):
    png = clean_floorplan(synthetic_plan())
    plan = tmp_path / "plan.png"
    plan.write_bytes(png)
    meta = floorplan_meta({"image": str(plan), "width_m": 12.0})
    assert meta["width_m"] == 12.0
    w, h = png_size(plan)
    assert meta["height_m"] == pytest.approx(12.0 * h / w, abs=0.01)
    assert floorplan_meta(None) is None
    assert floorplan_meta({"image": "/nonexistent.png"}) is None

    from hometwin.api import ApiServer
    from hometwin.simulate import build_simulation

    tracker, _ = build_simulation(seed=1)
    tracker.cfg.floorplan = {"image": str(plan), "width_m": 12.0}
    api = ApiServer(tracker, "127.0.0.1", 0)
    api.start()
    try:
        base = f"http://127.0.0.1:{api.port}"
        with urllib.request.urlopen(f"{base}/assets/floorplan", timeout=5) as r:
            assert r.read() == png
        with urllib.request.urlopen(f"{base}/overlay/map", timeout=5) as r:
            d = json.loads(r.read())
        assert d["floorplan"]["width_m"] == 12.0
    finally:
        api.stop()
