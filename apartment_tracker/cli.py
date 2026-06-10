"""CLI: run the tracker, query items, simulate, retrain, inspect plugins."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request


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
        with urllib.request.urlopen(url, timeout=5) as resp:
            entry = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"unknown item {args.item!r}" if e.code == 404 else f"error: {e}", file=sys.stderr)
        return 1
    if entry.get("status") == "never_seen":
        print(f"{entry['name']}: never seen")
    else:
        zone = entry.get("zone") or "outside known zones"
        pos = entry.get("position")
        print(
            f"{entry['name']}: {zone} at ({pos[0]}, {pos[1]}, {pos[2]}) "
            f"±{entry['sigma_m']} m, seen {entry['age_s']}s ago via {entry['last_sensor']}"
            + (" [stale]" if entry["status"] == "stale" else "")
        )
    return 0


def cmd_simulate(args) -> int:
    from apartment_tracker.simulate import run_simulation

    result = run_simulation(ticks=args.ticks, seed=args.seed)
    print(json.dumps(result, indent=2))
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
    wherep.set_defaults(fn=cmd_where)

    simp = sub.add_parser("simulate", help="run the synthetic apartment demo")
    simp.add_argument("--ticks", type=int, default=120)
    simp.add_argument("--seed", type=int, default=1)
    simp.set_defaults(fn=cmd_simulate)

    plugp = sub.add_parser("plugins", help="list available plugins")
    plugp.set_defaults(fn=cmd_plugins)

    trainp = sub.add_parser("train", help="run a trainer over a recorded dataset")
    trainp.add_argument("trainer", help="trainer plugin name, e.g. path_loss")
    trainp.add_argument("--dataset", default="dataset")
    trainp.add_argument("--records", default="rssi", help="record set name within the dataset")
    trainp.set_defaults(fn=cmd_train)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
