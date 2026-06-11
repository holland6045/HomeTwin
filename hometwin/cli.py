"""CLI: run the tracker, query items, simulate, retrain, inspect plugins."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request

TOKEN_ENV = "HOMETWIN_TOKEN"


def _auth_headers(args) -> dict:
    token = getattr(args, "token", None) or os.environ.get(TOKEN_ENV)
    return {"Authorization": f"Bearer {token}"} if token else {}


def cmd_run(args) -> int:
    from hometwin.api import ApiServer
    from hometwin.config import load_config
    from hometwin.tracker import Tracker

    cfg = load_config(args.config)
    tracker = Tracker(cfg)
    api = ApiServer(tracker, cfg.api_host, cfg.api_port)
    api.start()
    print(f"API on http://{cfg.api_host}:{api.port} — Ctrl-C to stop", file=sys.stderr)
    try:
        tracker.run()
    except KeyboardInterrupt:
        tracker.shutdown()
    finally:
        api.stop()
    return 0


def cmd_where(args) -> int:
    url = f"http://{args.host}:{args.port}/items/{urllib.parse.quote(args.item)}"
    try:
        req = urllib.request.Request(url, headers=_auth_headers(args))
        with urllib.request.urlopen(req, timeout=5) as resp:
            entry = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"unknown item {args.item!r}" if e.code == 404 else f"error: {e}", file=sys.stderr)
        return 1
    if entry.get("status") == "never_seen":
        print(f"{entry['name']}: never seen")
    else:
        zone = entry.get("spot") or entry.get("zone") or "outside known zones"
        if entry.get("maybe_in"):
            zone = f"likely inside {entry['maybe_in']} ({zone})"
        pos = entry.get("position")
        print(
            f"{entry['name']}: {zone} at ({pos[0]}, {pos[1]}, {pos[2]}) "
            f"±{entry['sigma_m']} m, seen {entry['age_s']}s ago via {entry['last_sensor']}"
            + (" [stale]" if entry["status"] == "stale" else "")
        )
    return 0


def cmd_simulate(args) -> int:
    from hometwin.simulate import run_simulation

    if args.serve:
        return _simulate_serve(args)
    result = run_simulation(ticks=args.ticks, seed=args.seed)
    print(json.dumps(result, indent=2))
    return 0


def _simulate_serve(args) -> int:
    import time

    from hometwin.api import ApiServer
    from hometwin.simulate import build_simulation

    tracker, state = build_simulation(seed=args.seed)
    api = ApiServer(tracker, "127.0.0.1", args.port)
    api.start()
    print(f"Live demo: http://127.0.0.1:{api.port}/ — Ctrl-C to stop", file=sys.stderr)
    try:
        while True:
            state.tick()
            tracker.step()
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        api.stop()
    return 0


def cmd_snapshot_map(args) -> int:
    """Render the map overlay to SVG — from a live tracker or the simulation."""
    from hometwin.render import map_svg

    if args.url:
        req = urllib.request.Request(f"{args.url.rstrip('/')}/overlay/map",
                                     headers=_auth_headers(args))
        with urllib.request.urlopen(req, timeout=10) as r:
            overlay = json.loads(r.read())
    else:
        from hometwin.overlay import map_overlay
        from hometwin.simulate import build_simulation

        tracker, state = build_simulation(seed=args.seed)
        for _ in range(args.ticks):
            state.tick()
            tracker.step()
        overlay = map_overlay(tracker)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(map_svg(overlay))
    print(args.output)
    return 0


def cmd_plugins(args) -> int:
    from hometwin import registry

    registry.load_plugins()
    for kind in registry.KINDS:
        print(f"{kind}: {', '.join(registry.available(kind)) or '(none)'}")
    return 0


def cmd_train(args) -> int:
    from hometwin import registry
    from hometwin.training.dataset import DatasetRecorder

    registry.load_plugins()
    recorder = DatasetRecorder(args.dataset)
    records = recorder.load(args.records)
    trainer = registry.create("trainer", args.trainer)
    result = trainer.train(records)
    print(json.dumps(result, indent=2))
    return 0


def cmd_update_splat(args) -> int:
    base = f"http://{args.host}:{args.port}"
    with open(args.file, "rb") as f:
        data = f.read()
    req = urllib.request.Request(
        f"{base}/assets/splat",
        data=data,
        method="POST",
        headers={"Content-Type": "application/octet-stream", **_auth_headers(args)},
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        print(resp.read().decode())
    if args.transform:
        treq = urllib.request.Request(
            f"{base}/assets/splat/transform",
            data=args.transform.encode(),
            method="POST",
            headers={"Content-Type": "application/json", **_auth_headers(args)},
        )
        with urllib.request.urlopen(treq, timeout=10) as resp:
            print(resp.read().decode())
    return 0


def cmd_capture_snapshots(args) -> int:
    """Grab one frame from each configured camera — scan input + calibration."""
    from pathlib import Path

    from hometwin.config import load_config

    try:
        import cv2
    except ImportError:
        print("capture-snapshots requires opencv: pip install hometwin[vision]",
              file=sys.stderr)
        return 1

    cfg = load_config(args.config)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    captured = 0
    for sensor in cfg.sensors:
        source = getattr(sensor, "frame_source", None)
        if source is None:
            continue
        try:
            if hasattr(source, "start"):
                source.start()
            frame = source.get_frame()
            if frame is None:
                print(f"# {sensor.sensor_id}: no frame", file=sys.stderr)
                continue
            path = out / f"{sensor.sensor_id}.jpg"
            if not cv2.imwrite(str(path), frame):
                print(f"# {sensor.sensor_id}: unencodable frame", file=sys.stderr)
                continue
            print(path)
            captured += 1
        except Exception as e:
            print(f"# {sensor.sensor_id}: {e}", file=sys.stderr)
        finally:
            if hasattr(source, "stop"):
                source.stop()
    return 0 if captured else 1


def cmd_webcam_setup(args) -> int:
    """Bootstrap the camera pose from one flat marker at a known position."""
    try:
        import cv2
    except ImportError:
        print("webcam-setup requires opencv: pip install hometwin[vision]", file=sys.stderr)
        return 1
    import time as _t

    import yaml

    from hometwin.detectors.aruco import ArucoDetector
    from hometwin.posefit import average_poses, camera_pose_from_marker

    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        print(f"cannot open camera {args.device!r}", file=sys.stderr)
        return 1
    det = ArucoDetector(dictionary=args.dictionary)
    detector = det._detector
    if args.board:
        from hometwin.board import MARKER_IDS, MARKER_MM

        from hometwin.posefit import camera_pose_from_board

        scale = (args.marker_mm / MARKER_MM) if args.marker_mm else 1.0
        wanted = set(MARKER_IDS)
        print(f"lay the origin board FLAT in view; collecting {args.frames} "
              f"sightings...", file=sys.stderr)
    else:
        wanted = {args.marker_id}
        print(f"lay marker {args.marker_id} FLAT at world {args.marker_pos}, top edge "
              f"toward +y; collecting {args.frames} sightings...", file=sys.stderr)
    poses = []
    t0 = _t.time()
    try:
        while len(poses) < args.frames and _t.time() - t0 < args.timeout:
            ok, frame = cap.read()
            if not ok:
                continue
            corners, ids, _ = detector.detectMarkers(frame)
            if ids is None:
                continue
            h, w = frame.shape[:2]
            if args.board:
                seen = {
                    int(mid): [tuple(pt) for pt in quad[0]]
                    for quad, mid in zip(corners, ids.flatten())
                    if int(mid) in wanted
                }
                if seen:
                    poses.append(camera_pose_from_board(seen, (w, h), args.hfov, scale))
                continue
            for quad, marker_id in zip(corners, ids.flatten()):
                if int(marker_id) != args.marker_id:
                    continue
                poses.append(camera_pose_from_marker(
                    [tuple(pt) for pt in quad[0]], (w, h), args.hfov,
                    tuple(args.marker_pos), args.marker_mm / 1000.0))
    finally:
        cap.release()
    if not poses:
        print(f"never saw marker {args.marker_id} — check lighting/print size",
              file=sys.stderr)
        return 1
    pose = average_poses(poses)
    if abs(pose["roll_deg"]) > 5.0:
        print(f"# WARNING: camera has {pose['roll_deg']:.1f} deg roll; "
              "the pinhole model ignores roll — mount it level", file=sys.stderr)
    print(f"# pose from {len(poses)} sightings — paste into your config:",
          file=sys.stderr)
    print(yaml.safe_dump([{
        "type": "camera",
        "id": "webcam",
        "position": [round(v, 3) for v in pose["position"]],
        "yaw_deg": round(pose["yaw_deg"], 2),
        "pitch_deg": round(pose["pitch_deg"], 2),
        "hfov_deg": args.hfov,
        "surface_z": float(args.marker_pos[2]),
        "source": {"type": "opencv", "device": args.device},
        "detector": {"type": "aruco", "dictionary": args.dictionary},
    }], sort_keys=False))
    return 0


def cmd_webcam_test(args) -> int:
    """Hardware smoke test: open the webcam, detect ArUco tags live."""
    try:
        import cv2
    except ImportError:
        print("webcam-test requires opencv: pip install hometwin[vision]", file=sys.stderr)
        return 1
    from hometwin.detectors.aruco import ArucoDetector

    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        print(f"cannot open camera {args.device!r} — check the device index "
              "(try --device 1) and that no other app holds the camera", file=sys.stderr)
        return 1
    det = ArucoDetector(dictionary=args.dictionary)
    import time as _t

    frames = hits = 0
    t0 = _t.time()
    print(f"watching device {args.device} for {args.seconds}s — show it a tag "
          f"(hometwin make-anchor --id 7)", file=sys.stderr)
    try:
        while _t.time() - t0 < args.seconds:
            ok, frame = cap.read()
            if not ok:
                continue
            frames += 1
            for d in det.detect(frame):
                hits += 1
                u, v = d.center
                print(f"{d.tag_id} at u={u:.3f} v={v:.3f}")
    finally:
        cap.release()
    dt = max(_t.time() - t0, 1e-6)
    print(f"# {frames} frames in {dt:.1f}s ({frames / dt:.1f} fps), "
          f"{hits} tag detections", file=sys.stderr)
    return 0 if frames else 1


def cmd_board_check(args) -> int:
    """Precision instrument: a calibrated camera measures the board.

    Lay the sheet anywhere — counter, shelf, floor. Reports the surface z,
    tilt, depth/scale self-check (suggests a corrected hfov), and prints
    ready-to-paste anchor/spot snippets at the measured world positions.
    """
    try:
        import cv2
    except ImportError:
        print("board-check requires opencv: pip install hometwin[vision]", file=sys.stderr)
        return 1
    import time as _t

    import yaml

    from hometwin.board import MARKER_IDS, MARKER_MM
    from hometwin.config import load_config
    from hometwin.detectors.aruco import ArucoDetector
    from hometwin.posefit import locate_board

    cfg = load_config(args.config)
    cam = next((s for s in cfg.sensors if s.sensor_id == args.camera
                and hasattr(s, "geometry")), None)
    if cam is None:
        cams = [s.sensor_id for s in cfg.sensors if hasattr(s, "geometry")]
        print(f"camera {args.camera!r} not in config (cameras: {cams})", file=sys.stderr)
        return 1
    import math as _m

    hfov = _m.degrees(2.0 * _m.atan(cam.geometry.tan_h))
    scale = (args.marker_mm / MARKER_MM) if args.marker_mm else 1.0
    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        print(f"cannot open camera {args.device!r}", file=sys.stderr)
        return 1
    detector = ArucoDetector(dictionary=args.dictionary)._detector
    wanted = set(MARKER_IDS)
    reports = []
    t0 = _t.time()
    try:
        while len(reports) < args.frames and _t.time() - t0 < args.timeout:
            ok, frame = cap.read()
            if not ok:
                continue
            corners, ids, _ = detector.detectMarkers(frame)
            if ids is None:
                continue
            seen = {int(m): [tuple(pt) for pt in q[0]]
                    for q, m in zip(corners, ids.flatten()) if int(m) in wanted}
            if seen:
                h, w = frame.shape[:2]
                reports.append(locate_board(
                    seen, cam.geometry, (w, h), hfov, scale,
                    known_surface_z=args.surface_z))
    finally:
        cap.release()
    if not reports:
        print("never saw the board", file=sys.stderr)
        return 1

    n = len(reports)
    z = sum(r["surface_z"] for r in reports) / n
    tilt = sum(r["tilt_deg"] for r in reports) / n
    origin = [round(sum(r["origin_world"][i] for r in reports) / n, 3) for i in range(3)]
    ratios = [r["scale_ratio"] for r in reports if r["scale_ratio"]]
    print(f"# board seen {n}x via {args.camera}", file=sys.stderr)
    print(f"# surface z = {z:.3f} m   tilt = {tilt:.2f} deg   origin = {origin}",
          file=sys.stderr)
    if ratios:
        ratio = sum(ratios) / len(ratios)
        mms = [r["suggested_marker_mm"] for r in reports if r["suggested_marker_mm"]]
        print(f"# print-scale ratio = {ratio:.4f} (1.0 = printed at 100%)",
              file=sys.stderr)
        if abs(ratio - 1.0) > 0.03 and mms:
            print(f"# >3% off vs the known surface: the sheet printed scaled — "
                  f"pass --marker-mm {sum(mms) / len(mms):.1f}", file=sys.stderr)
    if tilt > 3.0:
        print("# WARNING: board reads tilted — surface not flat or pose drift",
              file=sys.stderr)
    # ready-to-paste: the measured surface as a spot, the markers as anchors
    last = reports[-1]
    print(yaml.safe_dump({
        "spots": [{"name": args.name, "position": origin, "radius": 0.3}],
        "anchors": [
            {"tag": f"aruco:{mid}", "position": pos}
            for mid, pos in last["marker_positions_world"].items()
        ],
    }, sort_keys=False))
    return 0


def cmd_floorplan(args) -> int:
    """Extract + clean a floorplan from a listing URL (or image URL/file)."""
    from hometwin.floorplan import clean_floorplan, fetch, find_floorplan_url

    src = args.source
    if src.startswith(("http://", "https://")):
        data = fetch(src)
        if data[:4] not in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1") \
                and b"<" in data[:512]:
            url = find_floorplan_url(data.decode("utf-8", errors="replace"), src)
            if not url:
                print("no floorplan image found on that page — pass the image URL "
                      "directly", file=sys.stderr)
                return 1
            print(f"# found {url}", file=sys.stderr)
            data = fetch(url)
    else:
        with open(src, "rb") as f:
            data = f.read()
    try:
        png = clean_floorplan(data)
    except (RuntimeError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    with open(args.output, "wb") as f:
        f.write(png)
    print(args.output)
    print(f"""# config (world width measured/printed on the listing):
world:
  floorplan:
    image: {args.output}
    width_m: {args.width_m}
# world (0,0) = the plan's bottom-left; lay the origin board there and the
# frames coincide.""", file=sys.stderr)
    return 0


def cmd_make_board(args) -> int:
    """Generate the printable origin board (lay flat = world origin set)."""
    from hometwin.board import MARKER_IDS, board_svg
    from hometwin.tags import marker_bits

    try:
        bits = {mid: marker_bits(args.dictionary, mid) for mid in MARKER_IDS}
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    out = args.output or "origin-board.svg"
    with open(out, "w", encoding="utf-8") as f:
        f.write(board_svg(bits))
    print(out)
    print("# print at 100% scale, lay flat in camera view, then:\n"
          "#   hometwin webcam-setup --board", file=sys.stderr)
    return 0


def cmd_make_anchor(args) -> int:
    """Generate a printable blocky ArUco calibration target."""
    try:
        import cv2
    except ImportError:
        print("make-anchor requires opencv: pip install hometwin[vision]",
              file=sys.stderr)
        return 1
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, args.dictionary))
    marker = cv2.aruco.generateImageMarker(dictionary, args.id, args.pixels)
    border = args.pixels // 8  # quiet zone so detection survives dark walls
    marker = cv2.copyMakeBorder(
        marker, border, border, border, border, cv2.BORDER_CONSTANT, value=255
    )
    out = args.output or f"anchor-{args.id}.png"
    if not cv2.imwrite(out, marker):
        print(f"failed to write {out}", file=sys.stderr)
        return 1
    print(out)
    print(
        f"# print flat, mount rigid; then add to config:\n"
        f"# world:\n#   anchors:\n"
        f"#     - {{tag: \"aruco:{args.id}\", position: [x, y, z]}}",
        file=sys.stderr,
    )
    return 0


def cmd_make_tag(args) -> int:
    """Generate a styled, printable fiducial label (SVG)."""
    from hometwin.tags import marker_bits, tag_svg

    try:
        bits = marker_bits(args.dictionary, args.id)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    twin_bits = None
    if args.twin_id is not None:
        twin_bits = marker_bits(args.dictionary, args.twin_id)
    ident = args.ident or f"TAG/{args.id:02d}"
    svg = tag_svg(bits, ident, caption=args.caption, palette=args.palette,
                  size_mm=args.size_mm, layout=args.layout, twin=args.twin,
                  twin_bits=twin_bits)
    out = args.output or f"tag-{args.id}.svg"
    with open(out, "w", encoding="utf-8") as f:
        f.write(svg)
    print(out)
    print(f'# config: tags: ["aruco:{args.id}"]  (item)  or  '
          f'{{tag: "aruco:{args.id}", ...}}  (spot/anchor)', file=sys.stderr)
    return 0


def cmd_calibrate_cameras(args) -> int:
    import yaml

    from hometwin.calibration import calibrate_cameras

    with open(args.pairs, encoding="utf-8") as f:
        pairs = yaml.safe_load(f)
    result = calibrate_cameras(
        args.colmap, pairs, image_names=args.images or None, up_axis=args.up
    )
    align = result["alignment"]
    print(
        f"# alignment: scale={align['scale']:.4f} yaw={align['yaw_deg']:.2f}deg "
        f"residual={align['residual_m']:.3f}m",
        file=sys.stderr,
    )
    if align["residual_m"] > 0.15:
        print("# WARNING: alignment residual > 0.15 m — check reference points", file=sys.stderr)
    for cam in result["cameras"]:
        if abs(cam["roll_deg"]) > 5.0:
            print(
                f"# WARNING: {cam['image']} has {cam['roll_deg']}deg roll; "
                "the camera model ignores roll — mount it level",
                file=sys.stderr,
            )
        print(f"# from {cam['image']}")
        print(
            yaml.safe_dump(
                [
                    {
                        "type": "camera",
                        "id": cam["image"].rsplit(".", 1)[0],
                        "position": cam["position"],
                        "yaw_deg": cam["yaw_deg"],
                        "pitch_deg": cam["pitch_deg"],
                        "hfov_deg": cam["hfov_deg"],
                    }
                ],
                sort_keys=False,
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="hometwin")
    sub = p.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run the tracker from a config file")
    runp.add_argument("-c", "--config", required=True)
    runp.set_defaults(fn=cmd_run)

    wherep = sub.add_parser("where", help="ask a running tracker where an item is")
    wherep.add_argument("item")
    wherep.add_argument("--host", default="127.0.0.1")
    wherep.add_argument("--port", type=int, default=8080)
    wherep.add_argument("--token", help=f"API token (or set ${TOKEN_ENV})")
    wherep.set_defaults(fn=cmd_where)

    simp = sub.add_parser("simulate", help="run the synthetic apartment demo")
    simp.add_argument("--ticks", type=int, default=120)
    simp.add_argument("--seed", type=int, default=1)
    simp.add_argument("--serve", action="store_true", help="serve the live dashboard instead")
    simp.add_argument("--port", type=int, default=8080)
    simp.set_defaults(fn=cmd_simulate)

    snapp = sub.add_parser("snapshot-map", help="render the map overlay to an SVG file")
    snapp.add_argument("-o", "--output", default="map.svg")
    snapp.add_argument("--url", help="running tracker base URL (default: run the simulation)")
    snapp.add_argument("--token", help=f"API token (or set ${TOKEN_ENV})")
    snapp.add_argument("--ticks", type=int, default=120)
    snapp.add_argument("--seed", type=int, default=1)
    snapp.set_defaults(fn=cmd_snapshot_map)

    plugp = sub.add_parser("plugins", help="list available plugins")
    plugp.set_defaults(fn=cmd_plugins)

    setp = sub.add_parser(
        "webcam-setup", help="compute the camera pose from one flat marker (PnP)")
    setp.add_argument("--device", type=int, default=0)
    setp.add_argument("--marker-id", type=int, default=100)
    setp.add_argument("--marker-mm", type=float, default=100.0,
                      help="printed marker side length (black border included)")
    setp.add_argument("--board", action="store_true",
                      help="solve against the printed origin board (make-board); "
                           "no coordinates needed, --marker-mm only if not printed at 100%%")
    setp.add_argument("--marker-pos", type=float, nargs=3, default=[0.0, 0.0, 0.0],
                      metavar=("X", "Y", "Z"), help="marker center in world metres")
    setp.add_argument("--hfov", type=float, default=70.0, help="camera horizontal FOV")
    setp.add_argument("--frames", type=int, default=30)
    setp.add_argument("--timeout", type=float, default=30.0)
    setp.add_argument("--dictionary", default="DICT_4X4_250")
    setp.set_defaults(fn=cmd_webcam_setup)

    webp = sub.add_parser("webcam-test", help="open the webcam and detect tags live")
    webp.add_argument("--device", type=int, default=0)
    webp.add_argument("--seconds", type=int, default=15)
    webp.add_argument("--dictionary", default="DICT_4X4_250")
    webp.set_defaults(fn=cmd_webcam_test)

    bchk = sub.add_parser("board-check",
                          help="measure the board with a calibrated camera: "
                               "surface z, scale/depth check, anchor snippets")
    bchk.add_argument("-c", "--config", required=True)
    bchk.add_argument("--camera", default="webcam", help="camera id in the config")
    bchk.add_argument("--device", type=int, default=0)
    bchk.add_argument("--name", default="measured-surface", help="spot name to emit")
    bchk.add_argument("--marker-mm", type=float, help="printed size if not 100%%")
    bchk.add_argument("--surface-z", type=float,
                      help="known height of the surface the board lies on "
                           "(floor: 0) — enables the print-scale check")
    bchk.add_argument("--frames", type=int, default=20)
    bchk.add_argument("--timeout", type=float, default=30.0)
    bchk.add_argument("--dictionary", default="DICT_4X4_250")
    bchk.set_defaults(fn=cmd_board_check)

    flp = sub.add_parser("floorplan",
                         help="extract + clean a floorplan from a listing URL")
    flp.add_argument("source", help="listing URL, image URL, or local image file")
    flp.add_argument("--width-m", type=float, default=10.0,
                     help="real-world width of the plan in metres")
    flp.add_argument("-o", "--output", default="floorplan.png")
    flp.set_defaults(fn=cmd_floorplan)

    brdp = sub.add_parser("make-board",
                          help="printable origin board: lay flat, it IS the world origin")
    brdp.add_argument("--dictionary", default="DICT_4X4_250")
    brdp.add_argument("-o", "--output")
    brdp.set_defaults(fn=cmd_make_board)

    ancp = sub.add_parser("make-anchor", help="generate a printable ArUco calibration target")
    ancp.add_argument("--id", type=int, required=True, help="marker id (use 100+ for anchors)")
    ancp.add_argument("--dictionary", default="DICT_4X4_250")
    ancp.add_argument("--pixels", type=int, default=800)
    ancp.add_argument("-o", "--output")
    ancp.set_defaults(fn=cmd_make_anchor)

    tagp = sub.add_parser("make-tag", help="generate a styled printable fiducial label (SVG)")
    tagp.add_argument("--id", type=int, required=True, help="ArUco marker id")
    tagp.add_argument("--ident", help='big label text, e.g. "BOX/07" or "DRW/02"')
    tagp.add_argument("--caption", default="", help="vertical rail text, e.g. drawer name")
    tagp.add_argument("--palette", default="signal", choices=["signal", "cyan", "magenta", "acid"])
    tagp.add_argument("--dictionary", default="DICT_4X4_250")
    tagp.add_argument("--size-mm", type=float, default=60.0, help="printed width")
    tagp.add_argument("--layout", default="portrait", choices=["portrait", "wide"],
                      help="wide: 25:7 strip for shelf edges; marker keeps full height")
    tagp.add_argument("--twin", action="store_true",
                      help="wide only: repeat the SAME marker at both ends")
    tagp.add_argument("--twin-id", type=int,
                      help="wide only: DIFFERENT marker id for the right end — "
                           "preferred for anchors (each end is unambiguous)")
    tagp.add_argument("-o", "--output")
    tagp.set_defaults(fn=cmd_make_tag)

    calp = sub.add_parser(
        "calibrate-cameras",
        help="derive camera poses from a COLMAP reconstruction (splat scan)",
    )
    calp.add_argument("--colmap", required=True, help="dir containing cameras.txt/images.txt")
    calp.add_argument("--pairs", required=True, help="YAML list of {colmap: [...], world: [...]}")
    calp.add_argument("--images", nargs="*", help="snapshot filenames to calibrate (default all)")
    calp.add_argument("--up", choices=["z", "y"], default="z", help="scan up-axis")
    calp.set_defaults(fn=cmd_calibrate_cameras)

    upsp = sub.add_parser("update-splat", help="push a new splat scan to a running tracker")
    upsp.add_argument("file", help=".ply/.splat to upload")
    upsp.add_argument("--host", default="127.0.0.1")
    upsp.add_argument("--port", type=int, default=8080)
    upsp.add_argument("--transform", help='JSON splat_transform, e.g. \'{"scale": 1.8}\'')
    upsp.add_argument("--timeout", type=int, default=300)
    upsp.add_argument("--token", help=f"admin API token (or set ${TOKEN_ENV})")
    upsp.set_defaults(fn=cmd_update_splat)

    capp = sub.add_parser(
        "capture-snapshots", help="save one frame per configured camera (scan/calibration input)"
    )
    capp.add_argument("-c", "--config", required=True)
    capp.add_argument("-o", "--output", default="snapshots")
    capp.set_defaults(fn=cmd_capture_snapshots)

    trainp = sub.add_parser("train", help="run a trainer over a recorded dataset")
    trainp.add_argument("trainer", help="trainer plugin name, e.g. path_loss")
    trainp.add_argument("--dataset", default="dataset")
    trainp.add_argument("--records", default="rssi", help="record set name within the dataset")
    trainp.set_defaults(fn=cmd_train)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
