"""Server-side SVG rendering of the map overlay.

Same layer model the dashboard draws, rendered headless: development
snapshots, CI artifacts, chat-friendly previews of a live or simulated
tracker. Pure stdlib string assembly — no drawing library.
"""

from __future__ import annotations

ITEM_COLORS = ["#4ea1ff", "#ff7eb6", "#66d98c", "#c9a227", "#8a7dff", "#ff9d5c"]
W, H, PAD = 880.0, 660.0, 40.0


def _bounds(zones: list[dict]) -> tuple[float, float, float, float]:
    if not zones:
        return (0.0, 0.0, 10.0, 8.0)
    return (
        min(z["min"][0] for z in zones),
        min(z["min"][1] for z in zones),
        max(z["max"][0] for z in zones),
        max(z["max"][1] for z in zones),
    )


def _heat_color(v: float) -> str:
    """Blue->red ramp as rgba (hsl() support varies across SVG rasterizers)."""
    hue = (240.0 - 240.0 * v) / 60.0
    c, x = 0.9, 0.9 * (1.0 - abs(hue % 2.0 - 1.0))
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(hue) % 6]
    to255 = lambda f: int((f + 0.55 - 0.9 / 2.0) * 255 / 1.0)
    return (
        f"rgba({to255(r)},{to255(g)},{to255(b)},{0.15 + 0.5 * v:.2f})"
    )


def map_svg(d: dict) -> str:
    xmin, ymin, xmax, ymax = _bounds(d.get("zones", []))
    s = min((W - 2 * PAD) / (xmax - xmin), (H - 2 * PAD) / (ymax - ymin))

    def T(p) -> tuple[float, float]:
        return (PAD + (p[0] - xmin) * s, H - PAD - (p[1] - ymin) * s)

    el: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}" '
        f'font-family="system-ui, sans-serif">',
        f'<rect width="{W:.0f}" height="{H:.0f}" fill="#14171c"/>',
    ]

    for z in d.get("zones", []):
        x0, y0 = T(z["min"])
        x1, y1 = T(z["max"])
        el.append(
            f'<rect x="{x1 if x1 < x0 else x0:.1f}" y="{y1:.1f}" width="{abs(x1 - x0):.1f}" '
            f'height="{abs(y0 - y1):.1f}" fill="none" stroke="#39414e"/>'
        )
        el.append(f'<text x="{x0 + 4:.1f}" y="{y0 - 4:.1f}" font-size="11" '
                  f'fill="#566073">{z["name"]}</text>')

    for hm in d.get("heatmaps", []):
        if not hm.get("values"):
            continue
        cell = hm["cell_m"]
        for iy, row in enumerate(hm["values"]):
            for ix, v in enumerate(row):
                if v < 0.05:
                    continue
                px, py = T((hm["bounds"][0] + ix * cell, hm["bounds"][1] + (iy + 1) * cell))
                el.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{cell * s + 0.5:.1f}" '
                          f'height="{cell * s + 0.5:.1f}" fill="{_heat_color(v)}"/>')
        for node in (hm.get("nodes") or {}).values():
            px, py = T(node)
            el.append(f'<rect x="{px - 2:.1f}" y="{py - 2:.1f}" width="4" height="4" fill="#8a7dff"/>')

    for r in d.get("rings", []):
        cx, cy = T((r["cx"], r["cy"]))
        el.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r["radius"] * s:.1f}" fill="none" '
                  f'stroke="rgba(102,217,140,0.45)" stroke-width="1.5"/>')
        ax, ay = T(r["anchor"])
        el.append(f'<rect x="{ax - 3:.1f}" y="{ay - 3:.1f}" width="6" height="6" fill="#66d98c"/>')

    for b in d.get("bearings", []):
        ox, oy = T(b["origin"])
        ex, ey = T((b["origin"][0] + b["direction"][0] * b["length_m"],
                    b["origin"][1] + b["direction"][1] * b["length_m"]))
        el.append(f'<line x1="{ox:.1f}" y1="{oy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
                  f'stroke="rgba(201,162,39,0.6)" stroke-width="1.5" stroke-dasharray="6 4"/>')

    item_ids = [i["item_id"] for i in d.get("items", [])]

    def color_of(item_id: str) -> str:
        idx = item_ids.index(item_id) if item_id in item_ids else 0
        return ITEM_COLORS[idx % len(ITEM_COLORS)]

    for tr in d.get("trails", []):
        pts = " ".join(f"{T(p)[0]:.1f},{T(p)[1]:.1f}" for p in tr["points"])
        el.append(f'<polyline points="{pts}" fill="none" stroke="{color_of(tr["item_id"])}" '
                  f'stroke-width="2" opacity="0.45"/>')

    for m in d.get("movables", []):
        pts = " ".join(f"{T(p)[0]:.1f},{T(p)[1]:.1f}" for p in m["path"])
        el.append(f'<polyline points="{pts}" fill="none" stroke="#39414e" stroke-width="2"/>')
        tx, ty = T(m["tag_pos"])
        color = "#9dff00" if m["is_open"] else "#566073"
        el.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="4" fill="{color}"/>')
        state = "OPEN" if m["is_open"] else "closed"
        el.append(f'<text x="{tx + 7:.1f}" y="{ty + 4:.1f}" font-size="10" '
                  f'fill="#7d8696">{m["name"]} {state}</text>')

    for sp in d.get("spots", []):
        px, py = T(sp["position"])
        el.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{max(sp["radius"] * s, 3):.1f}" '
                  f'fill="none" stroke="rgba(157,255,0,0.5)"/>')
        el.append(f'<rect x="{px - 3:.1f}" y="{py - 3:.1f}" width="6" height="6" fill="#9dff00" '
                  f'transform="rotate(45 {px:.1f} {py:.1f})"/>')
        el.append(f'<text x="{px + 7:.1f}" y="{py - 5:.1f}" font-size="10" '
                  f'fill="#7da356">{sp["name"]}</text>')

    for a in d.get("anchors", []):
        px, py = T(a["position"])
        el.append(f'<rect x="{px - 5:.1f}" y="{py - 5:.1f}" width="10" height="10" fill="#e8e8e8"/>')
        el.append(f'<text x="{px + 8:.1f}" y="{py + 4:.1f}" font-size="10" '
                  f'fill="#9aa3b2">{a["tag"]}</text>')

    if d.get("presence"):
        px, py = T(d["presence"]["centroid"])
        el.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{d["presence"]["sigma_m"] * s:.1f}" '
                  f'fill="rgba(138,125,255,0.25)"/>')
        el.append(f'<text x="{px + 6:.1f}" y="{py:.1f}" font-size="11" fill="#8a7dff">presence</text>')

    for c in d.get("cameras", []):
        px, py = T(c["position"])
        cal = c.get("calibration")
        color = "#e05c5c" if cal and cal.get("healthy") is False else "#c9a227"
        el.append(f'<polygon points="0,0 14,-6 14,6" fill="{color}" '
                  f'transform="translate({px:.1f} {py:.1f}) rotate({-c["yaw_deg"]:.1f})"/>')
        el.append(f'<text x="{px:.1f}" y="{py + 16:.1f}" font-size="9" '
                  f'fill="#7d8696">{c["sensor_id"]}</text>')

    for it in d.get("items", []):
        if not it.get("position"):
            continue
        px, py = T(it["position"])
        stale = it["status"] == "stale"
        color = "#e0a23c" if stale else color_of(it["item_id"])
        el.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{max(it["sigma_m"] * s, 4):.1f}" '
                  f'fill="none" stroke="{color}" opacity="0.55"/>')
        el.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{color}"/>')
        where = it.get("spot") or it.get("zone") or ""
        el.append(f'<text x="{px + 8:.1f}" y="{py - 6:.1f}" font-size="12" fill="#d9dee7">'
                  f'{it["name"]}</text>')
        el.append(f'<text x="{px + 8:.1f}" y="{py + 7:.1f}" font-size="9" fill="#7d8696">'
                  f'{where} ±{it["sigma_m"]}m</text>')

    el.append("</svg>")
    return "\n".join(el)
