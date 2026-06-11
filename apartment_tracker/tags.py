"""Designed fiducial labels: functional ArUco cores wrapped in a graphic
plate — dark panel, neon accent, hazard stripes, oversized mono ID type,
corner registration notches. Tags you'd actually put on visible furniture:
readable by the tracker, legible to humans, and styled after sci-fi
industrial UI (Marathon / antireal-school graphic design) rather than a
bare barcode block.

The SVG is vector throughout, so it prints crisp at any size. Only the bit
matrix needs OpenCV (CLI side); `tag_svg` itself is pure Python and
testable without it.
"""

from __future__ import annotations

PALETTES = {
    "signal": "#ffd400",  # signal yellow
    "cyan": "#19e3ff",
    "magenta": "#ff2e88",
    "acid": "#9dff00",
}

INK = "#0d0f12"
PAPER = "#f4f2ec"


def marker_bits(dictionary: str, marker_id: int) -> list[list[int]]:
    """ArUco bit matrix (1 = black cell), including the 1-cell black border.

    Requires opencv-contrib; the matrix is sampled from a 1-px-per-cell
    render so the SVG cells are exact.
    """
    try:
        import cv2
    except ImportError as e:
        raise RuntimeError(
            "tag generation requires opencv: pip install apartment-tracker[vision]"
        ) from e
    d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dictionary))
    side = d.markerSize + 2  # data cells + black border
    img = cv2.aruco.generateImageMarker(d, marker_id, side)
    return [[1 if img[y][x] < 128 else 0 for x in range(side)] for y in range(side)]


def _hazard_stripes(x: float, y: float, w: float, h: float, color: str, n: int = 6) -> str:
    step = w / n
    parts = [f'<clipPath id="hz"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>']
    parts.append(f'<g clip-path="url(#hz)">')
    for i in range(-1, n + 1):
        x0 = x + i * step
        parts.append(
            f'<polygon points="{x0},{y + h} {x0 + step / 2},{y + h} '
            f'{x0 + step / 2 + h},{y} {x0 + h},{y}" fill="{color}"/>'
        )
    parts.append("</g>")
    return "".join(parts)


def tag_svg(
    bits: list[list[int]],
    ident: str,
    caption: str = "",
    palette: str = "signal",
    size_mm: float = 60.0,
) -> str:
    """Render a styled label around an ArUco bit matrix.

    Layout (portrait plate, 3:4): marker on a paper field upper-left,
    hazard strip top-right, vertical caption rail on the left, oversized
    ID type at the bottom, registration notches in all corners.
    """
    if palette not in PALETTES:
        raise ValueError(f"unknown palette {palette!r}; choose from {sorted(PALETTES)}")
    accent = PALETTES[palette]
    W, H = 600.0, 800.0
    n = len(bits)

    # marker field: white quiet zone with the bit grid inside
    field_x, field_y, field_w = 120.0, 120.0, 400.0
    quiet = field_w / (n + 2)  # one-cell quiet zone all around
    cell = (field_w - 2 * quiet) / n
    cells = [
        f'<rect x="{field_x + quiet + ix * cell:.2f}" y="{field_y + quiet + iy * cell:.2f}" '
        f'width="{cell:.2f}" height="{cell:.2f}" fill="{INK}"/>'
        for iy, row in enumerate(bits)
        for ix, bit in enumerate(row)
        if bit
    ]

    notch = (
        '<g fill="{c}">'
        '<rect x="20" y="20" width="26" height="8"/><rect x="20" y="20" width="8" height="26"/>'
        '<rect x="554" y="20" width="26" height="8"/><rect x="572" y="20" width="8" height="26"/>'
        '<rect x="20" y="772" width="26" height="8"/><rect x="20" y="754" width="8" height="26"/>'
        '<rect x="554" y="772" width="26" height="8"/><rect x="572" y="754" width="8" height="26"/>'
        "</g>"
    ).format(c=accent)

    caption_text = (caption or ident).upper()
    mono = "font-family='ui-monospace, Menlo, monospace'"

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}"
     width="{size_mm}mm" height="{size_mm * H / W:.1f}mm">
  <rect width="{W:.0f}" height="{H:.0f}" fill="{INK}"/>
  <rect x="6" y="6" width="{W - 12:.0f}" height="{H - 12:.0f}" fill="none"
        stroke="{accent}" stroke-width="3"/>
  {notch}
  {_hazard_stripes(380, 40, 180, 36, accent)}
  <rect x="40" y="120" width="36" height="400" fill="{accent}"/>
  <text x="0" y="0" {mono} font-size="24" fill="{INK}" letter-spacing="3"
        transform="translate(66 508) rotate(-90)">{caption_text}</text>
  <rect x="{field_x:.0f}" y="{field_y:.0f}" width="{field_w:.0f}" height="{field_w:.0f}"
        fill="{PAPER}"/>
  {"".join(cells)}
  <rect x="40" y="580" width="520" height="4" fill="{accent}"/>
  <text x="40" y="700" {mono} font-size="96" font-weight="700"
        fill="{accent}" letter-spacing="2">{ident.upper()}</text>
  <text x="40" y="748" {mono} font-size="22" fill="#7d8696"
        letter-spacing="6">APT.TRACKER // FIDUCIAL</text>
</svg>
"""
