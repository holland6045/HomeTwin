"""The origin board: a printable calibration sheet that IS the world frame.

Three markers at factory-known offsets on one page (Bambu-encoder-plate
spirit, apartment-scale density). The user never types a coordinate: the
board's origin-marker center is world (0, 0, surface), printed arrows show
+x and +y, and any camera that sees the sheet — even partially, one marker
is enough, three is better — solves its own pose against up to 12 corner
correspondences.

The same layout constants drive the SVG generator and the PnP solver, so
the print and the math can never disagree (only the global print scale
needs measuring: one number, the origin marker's black width).
"""

from __future__ import annotations

MARKER_IDS = (200, 201, 202)  # origin, +x, +y
MARKER_MM = 60.0  # black square width at 100% print scale
SPACING_MM = 110.0  # center-to-center along each axis
SHEET_W_MM, SHEET_H_MM = 200.0, 200.0  # fits A4 and US-letter at 100%


def board_layout(scale: float = 1.0) -> dict:
    """Marker centers in board/world meters. `scale` corrects for printers
    that refuse 100%: measured_marker_mm / MARKER_MM."""
    s = SPACING_MM / 1000.0 * scale
    return {
        "marker_size_m": MARKER_MM / 1000.0 * scale,
        "centers": {
            MARKER_IDS[0]: (0.0, 0.0),
            MARKER_IDS[1]: (s, 0.0),
            MARKER_IDS[2]: (0.0, s),
        },
    }


def marker_corners_board(center: tuple[float, float], size_m: float):
    """Corner positions (z=0) in ArUco order, top edge toward +y."""
    h = size_m / 2.0
    cx, cy = center
    return [
        (cx - h, cy + h, 0.0),
        (cx + h, cy + h, 0.0),
        (cx + h, cy - h, 0.0),
        (cx - h, cy - h, 0.0),
    ]


def board_object_points(detected_ids, scale: float = 1.0) -> list[list]:
    """World-frame corner coordinates for the detected subset, ArUco order
    per marker, matching the order of `detected_ids`."""
    layout = board_layout(scale)
    pts = []
    for marker_id in detected_ids:
        center = layout["centers"].get(int(marker_id))
        if center is None:
            raise KeyError(f"marker {marker_id} is not on the origin board")
        pts.append(marker_corners_board(center, layout["marker_size_m"]))
    return pts


INK = "#0d0f12"
PAPER = "#ffffff"
ACCENT = "#ffd400"


def _marker_field_svg(bits, x_mm: float, y_mm: float, size_mm: float) -> str:
    n = len(bits)
    cell = size_mm / n
    parts = [
        f'<rect x="{x_mm - cell:.2f}" y="{y_mm - cell:.2f}" '
        f'width="{size_mm + 2 * cell:.2f}" height="{size_mm + 2 * cell:.2f}" fill="{PAPER}"/>'
    ]
    parts += [
        f'<rect x="{x_mm + ix * cell:.3f}" y="{y_mm + iy * cell:.3f}" '
        f'width="{cell:.3f}" height="{cell:.3f}" fill="{INK}"/>'
        for iy, row in enumerate(bits)
        for ix, bit in enumerate(row)
        if bit
    ]
    return "".join(parts)


def board_svg(bits_by_id: dict[int, list[list[int]]]) -> str:
    """Render the origin board. SVG y grows downward while board +y grows
    upward, so the sheet maps board (0,0) to its lower-left region.

    Print at 100% scale; verify by measuring the origin marker's black
    square (should be exactly MARKER_MM)."""
    for marker_id in MARKER_IDS:
        if marker_id not in bits_by_id:
            raise KeyError(f"missing bits for board marker {marker_id}")
    W, H = SHEET_W_MM, SHEET_H_MM
    margin = 25.0
    origin_x = margin + MARKER_MM / 2.0  # sheet mm of board (0, 0)
    origin_y = H - margin - MARKER_MM / 2.0

    def field(marker_id, bx_mm, by_mm):
        x = origin_x + bx_mm - MARKER_MM / 2.0
        y = origin_y - by_mm - MARKER_MM / 2.0
        return _marker_field_svg(bits_by_id[marker_id], x, y, MARKER_MM)

    arrow_y = origin_y + MARKER_MM / 2.0 + 8
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}"
     width="{W}mm" height="{H}mm" font-family="ui-monospace, Menlo, monospace">
  <rect width="{W}" height="{H}" fill="{PAPER}"/>
  <rect x="3" y="3" width="{W - 6}" height="{H - 6}" fill="none" stroke="{INK}" stroke-width="0.6"/>
  <text x="{margin}" y="14" font-size="7" font-weight="700" fill="{INK}"
        letter-spacing="1.5">HOMETWIN // ORIGIN BOARD</text>
  <text x="{margin}" y="21" font-size="3.2" fill="#555">lay flat in camera view — the {MARKER_IDS[0]} marker center IS world (0,0).
    print at 100%; the black squares must measure {MARKER_MM:.0f} mm.</text>
  {field(MARKER_IDS[0], 0.0, 0.0)}
  {field(MARKER_IDS[1], SPACING_MM, 0.0)}
  {field(MARKER_IDS[2], 0.0, SPACING_MM)}
  <g stroke="{INK}" stroke-width="0.9" fill="{INK}" font-size="5">
    <line x1="{origin_x + MARKER_MM / 2 + 6}" y1="{origin_y}"
          x2="{origin_x + SPACING_MM - MARKER_MM / 2 - 6}" y2="{origin_y}"/>
    <polygon points="{origin_x + SPACING_MM - MARKER_MM / 2 - 5},{origin_y - 2}
      {origin_x + SPACING_MM - MARKER_MM / 2 - 1},{origin_y}
      {origin_x + SPACING_MM - MARKER_MM / 2 - 5},{origin_y + 2}"/>
    <text x="{origin_x + SPACING_MM / 2 - 4}" y="{arrow_y}">+x</text>
    <line x1="{origin_x}" y1="{origin_y - MARKER_MM / 2 - 6}"
          x2="{origin_x}" y2="{origin_y - SPACING_MM + MARKER_MM / 2 + 6}"/>
    <polygon points="{origin_x - 2},{origin_y - SPACING_MM + MARKER_MM / 2 + 5}
      {origin_x},{origin_y - SPACING_MM + MARKER_MM / 2 + 1}
      {origin_x + 2},{origin_y - SPACING_MM + MARKER_MM / 2 + 5}"/>
    <text x="{origin_x + 5}" y="{origin_y - SPACING_MM / 2}">+y</text>
  </g>
  <rect x="{margin}" y="{H - 12}" width="40" height="4" fill="{ACCENT}"/>
  <text x="{margin + 44}" y="{H - 8.5}" font-size="3.2" fill="#555">markers {MARKER_IDS[0]}/{MARKER_IDS[1]}/{MARKER_IDS[2]} · spacing {SPACING_MM:.0f} mm · DICT_4X4_250</text>
</svg>
"""
