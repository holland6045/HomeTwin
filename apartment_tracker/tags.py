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


def _hazard_stripes(
    x: float, y: float, w: float, h: float, color: str, n: int = 6, clip_id: str = "hz"
) -> str:
    step = w / n
    parts = [
        f'<clipPath id="{clip_id}"><rect x="{x}" y="{y}" width="{w}" height="{h}"/></clipPath>'
    ]
    parts.append(f'<g clip-path="url(#{clip_id})">')
    for i in range(-1, n + 1):
        x0 = x + i * step
        parts.append(
            f'<polygon points="{x0},{y + h} {x0 + step / 2},{y + h} '
            f'{x0 + step / 2 + h},{y} {x0 + h},{y}" fill="{color}"/>'
        )
    parts.append("</g>")
    return "".join(parts)


def _marker_field(bits: list[list[int]], x: float, y: float, side: float) -> str:
    """White quiet-zone field with the bit grid inside; cell-exact rects."""
    n = len(bits)
    quiet = side / (n + 2)
    cell = (side - 2 * quiet) / n
    parts = [f'<rect x="{x:.0f}" y="{y:.0f}" width="{side:.0f}" height="{side:.0f}" fill="{PAPER}"/>']
    parts += [
        f'<rect x="{x + quiet + ix * cell:.2f}" y="{y + quiet + iy * cell:.2f}" '
        f'width="{cell:.2f}" height="{cell:.2f}" fill="{INK}"/>'
        for iy, row in enumerate(bits)
        for ix, bit in enumerate(row)
        if bit
    ]
    return "".join(parts)


def tag_svg(
    bits: list[list[int]],
    ident: str,
    caption: str = "",
    palette: str = "signal",
    size_mm: float = 60.0,
    layout: str = "portrait",
    twin: bool = False,
    twin_bits: list[list[int]] | None = None,
) -> str:
    """Render a styled label around an ArUco bit matrix.

    layout="portrait" (3:4 plate): marker upper-left, hazard strip
    top-right, vertical caption rail, oversized ID type at the bottom.

    layout="wide" (25:7 strip for shelf edges and drawer fronts): the
    marker spans nearly the full plate height — camera read range is set
    by marker size, so the strip shrinks around it instead of shrinking
    it. `twin=True` repeats the marker at the far end so a partially
    occluded edge still reads; pass `twin_bits` (a DIFFERENT marker ID)
    to make the right end distinct — preferred for calibration anchors,
    since each end becomes an ordinary unambiguous reference point.

    size_mm is the printed width; height follows the aspect.
    """
    if palette not in PALETTES:
        raise ValueError(f"unknown palette {palette!r}; choose from {sorted(PALETTES)}")
    if layout not in ("portrait", "wide"):
        raise ValueError(f"unknown layout {layout!r}; choose portrait or wide")
    accent = PALETTES[palette]
    caption_text = (caption or ident).upper()
    mono = "font-family='ui-monospace, Menlo, monospace'"

    if layout == "wide":
        W, H = 1000.0, 280.0
        side = 220.0  # marker height ~= plate height: visibility first
        notch = (
            '<g fill="{c}">'
            '<rect x="14" y="14" width="22" height="6"/><rect x="14" y="14" width="6" height="22"/>'
            '<rect x="964" y="14" width="22" height="6"/><rect x="980" y="14" width="6" height="22"/>'
            '<rect x="14" y="260" width="22" height="6"/><rect x="14" y="244" width="6" height="22"/>'
            '<rect x="964" y="260" width="22" height="6"/><rect x="980" y="244" width="6" height="22"/>'
            "</g>"
        ).format(c=accent)
        if twin_bits is not None:
            twin = True
        second = (
            _marker_field(twin_bits if twin_bits is not None else bits, W - 30 - side, 30, side)
            if twin
            else ""
        )
        text_right = W - 30 - side - 30 if twin else W - 40
        sub = f"{caption_text} // APT.TRACKER"
        if 280 + len(sub) * (22 * 0.62 + 5) > text_right:  # would crowd the marker
            sub = caption_text
        stripes = (
            ""
            if twin
            else _hazard_stripes(W - 200, 30, 160, 24, accent, clip_id="hzw")
        )
        return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}"
     width="{size_mm}mm" height="{size_mm * H / W:.1f}mm">
  <rect width="{W:.0f}" height="{H:.0f}" fill="{INK}"/>
  <rect x="5" y="5" width="{W - 10:.0f}" height="{H - 10:.0f}" fill="none"
        stroke="{accent}" stroke-width="3"/>
  {notch}
  {stripes}
  {_marker_field(bits, 30, 30, side)}
  {second}
  <clipPath id="txt"><rect x="280" y="0" width="{text_right - 290:.0f}" height="{H:.0f}"/></clipPath>
  <rect x="280" y="200" width="{text_right - 280:.0f}" height="4" fill="{accent}"/>
  <g clip-path="url(#txt)">
    <text x="280" y="160" {mono} font-size="92" font-weight="700"
          fill="{accent}" letter-spacing="2">{ident.upper()}</text>
    <text x="280" y="242" {mono} font-size="22" fill="#7d8696"
          letter-spacing="5">{sub}</text>
  </g>
</svg>
"""

    W, H = 600.0, 800.0
    notch = (
        '<g fill="{c}">'
        '<rect x="20" y="20" width="26" height="8"/><rect x="20" y="20" width="8" height="26"/>'
        '<rect x="554" y="20" width="26" height="8"/><rect x="572" y="20" width="8" height="26"/>'
        '<rect x="20" y="772" width="26" height="8"/><rect x="20" y="754" width="8" height="26"/>'
        '<rect x="554" y="772" width="26" height="8"/><rect x="572" y="754" width="8" height="26"/>'
        "</g>"
    ).format(c=accent)

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
  {_marker_field(bits, 120, 120, 400)}
  <rect x="40" y="580" width="520" height="4" fill="{accent}"/>
  <text x="40" y="700" {mono} font-size="96" font-weight="700"
        fill="{accent}" letter-spacing="2">{ident.upper()}</text>
  <text x="40" y="748" {mono} font-size="22" fill="#7d8696"
        letter-spacing="6">APT.TRACKER // FIDUCIAL</text>
</svg>
"""
