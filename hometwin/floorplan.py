"""Floorplan import: apartment-listing URL -> cleaned 2D map baseline.

Listings almost always carry a floorplan image. `hometwin floorplan <url>`
finds it (img-tag scoring), cleans it into a crisp black-on-white plan
(threshold, despeckle, autocrop), and registers it as the map background:
world (0, 0) maps to the image's bottom-left corner, scaled by one number
you do know — the unit's real width in metres (listings print it).

The plan is presentation + zone-authoring aid; tracking never depends on
it. Pair it with the origin board: lay the board at the plan's bottom-left
corner and the two frames coincide.
"""

from __future__ import annotations

import re
import struct
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"""([a-zA-Z-]+)\s*=\s*["']([^"']*)["']""")
_HINTS = ("floorplan", "floor-plan", "floor_plan", "fplan", "floor", "plan", "layout")
_ANTI = ("logo", "icon", "map-pin", "avatar", "thumb")


def score_image_tag(attrs: dict) -> int:
    """Floorplan-ness of one <img>: keyword hits in src/alt/class/id."""
    haystack = " ".join(
        attrs.get(k, "").lower() for k in ("src", "data-src", "alt", "class", "id", "title")
    )
    score = 0
    for hint in _HINTS:
        if hint in haystack:
            score += 10 if "plan" in hint else 3
    for anti in _ANTI:
        if anti in haystack:
            score -= 8
    if haystack.strip():
        score += 1  # any attributes at all beats a bare tag
    return score


def find_floorplan_url(html: str, base_url: str) -> str | None:
    """Best floorplan candidate from a listing page, absolute URL."""
    best, best_score = None, 0
    for tag in _IMG_RE.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        src = attrs.get("src") or attrs.get("data-src")
        if not src:
            continue
        score = score_image_tag(attrs)
        if score > best_score:
            best, best_score = src, score
    return urljoin(base_url, best) if best else None


def fetch(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "hometwin-floorplan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clean_floorplan(image_bytes: bytes, margin_px: int = 12) -> bytes:
    """Listing image -> crisp black-on-white plan: grayscale, adaptive
    threshold, speckle removal, autocrop to drawn content. Returns PNG."""
    try:
        import cv2
        import numpy as np
    except ImportError as e:
        raise RuntimeError(
            "floorplan cleanup requires opencv: pip install hometwin[vision]"
        ) from e
    raw = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(raw, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("not a decodable image")
    img = cv2.medianBlur(img, 3)
    binary = cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 31, 12
    )
    # drop isolated specks (scan noise, dithering) but keep thin walls
    inverted = 255 - binary
    n, labels, stats, _ = cv2.connectedComponentsWithStats(inverted, connectivity=8)
    keep = np.zeros_like(inverted)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 12:
            keep[labels == i] = 255
    ys, xs = np.nonzero(keep)
    if len(xs) == 0:
        raise ValueError("no drawn content found — is this really a floorplan?")
    x0 = max(int(xs.min()) - margin_px, 0)
    x1 = min(int(xs.max()) + margin_px, keep.shape[1] - 1)
    y0 = max(int(ys.min()) - margin_px, 0)
    y1 = min(int(ys.max()) + margin_px, keep.shape[0] - 1)
    cleaned = 255 - keep[y0:y1 + 1, x0:x1 + 1]
    ok, png = cv2.imencode(".png", cleaned)
    if not ok:
        raise ValueError("png encode failed")
    return png.tobytes()


def png_size(path: str | Path) -> tuple[int, int]:
    """(width, height) from the PNG IHDR — stdlib, no decoder needed."""
    with open(path, "rb") as f:
        header = f.read(26)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG")
    w, h = struct.unpack(">II", header[16:24])
    return int(w), int(h)


def floorplan_meta(cfg_floorplan: dict | None) -> dict | None:
    """Overlay metadata: world extent of the plan image, for the map view.
    World (0,0) = image bottom-left; height follows the aspect ratio."""
    if not cfg_floorplan or not cfg_floorplan.get("image"):
        return None
    try:
        w_px, h_px = png_size(cfg_floorplan["image"])
    except (OSError, ValueError):
        return None
    width_m = float(cfg_floorplan.get("width_m", 10.0))
    return {
        "width_m": width_m,
        "height_m": round(width_m * h_px / w_px, 3),
        "px": [w_px, h_px],
    }
