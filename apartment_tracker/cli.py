"""CLI: run the tracker, query items, simulate, retrain, inspect plugins."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request

TOKEN_ENV = "APARTMENT_TRACKER_TOKEN"


def _auth_headers(args) -> dict:
    token = getattr(args, "token", None) or os.environ.get(TOKEN_ENV)
    return {"Authorization": f"Bearer {token}"} if token else {}


def cmd_run(args) -> int:
    from apartment_tracker.api import ApiServer
    from apartment_tracker.config import load_config
    from apartment_tracker.tracker import Tracker

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
    from apartment_tracker.simulate import run_simulation

    if args.serve:
        return _simulate_serve(args)
    result = run_simulation(ticks=args.ticks, seed=args.seed)
    print(json.dumps(result, indent=2))
    return 0


def _simulate_serve(args) -> int:
    import time

    from apartment_tracker.api import ApiServer
    from apartment_tracker.simulate import build_simulation

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
    from apartment_tracker.render import map_svg

    if args.url:
        req = urllib.request.Request(f"{args.url.rstrip('/')}/overlay/map",
                                     headers=_auth_headers(args))
        with urllib.request.urlopen(req, timeout=10) as r:
            overlay = json.loads(r.read())
    else:
        from apartment_tracker.overlay import map_overlay
        from apartment_tracker.simulate import build_simulation

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
    from apartment_tracker import registry

    registry.load_plugins()
    for kind in registry.KINDS:
        print(f"{kind}: {', '.join(registry.available(kind)) or '(none)'}")
    return 0


def cmd_train(args) -> int:
    from apartment_tracker import registry
    from apartment_tracker.training.dataset import DatasetRecorder

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

    from apartment_tracker.config import load_config

    try:
        import cv2
    except ImportError:
        print("capture-snapshots requires opencv: pip install apartment-tracker[vision]",
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


def cmd_make_anchor(args) -> int:
    """Generate a printable blocky ArUco calibration target."""
    try:
        import cv2
    except ImportError:
        print("make-anchor requires opencv: pip install apartment-tracker[vision]",
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
    from apartment_tracker.tags import marker_bits, tag_svg

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

    from apartment_tracker.calibration import calibrate_cameras

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
    p = argparse.ArgumentParser(prog="apartment-tracker")
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

    ancp = sub.add_parser("make-anchor", help="generate a printable ArUco calibration target")
    ancp.add_argument("--id", type=int, required=True, help="marker id (use 100+ for anchors)")
    ancp.add_argument("--dictionary", default="DICT_4X4_50")
    ancp.add_argument("--pixels", type=int, default=800)
    ancp.add_argument("-o", "--output")
    ancp.set_defaults(fn=cmd_make_anchor)

    tagp = sub.add_parser("make-tag", help="generate a styled printable fiducial label (SVG)")
    tagp.add_argument("--id", type=int, required=True, help="ArUco marker id")
    tagp.add_argument("--ident", help='big label text, e.g. "BOX/07" or "DRW/02"')
    tagp.add_argument("--caption", default="", help="vertical rail text, e.g. drawer name")
    tagp.add_argument("--palette", default="signal", choices=["signal", "cyan", "magenta", "acid"])
    tagp.add_argument("--dictionary", default="DICT_4X4_50")
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
