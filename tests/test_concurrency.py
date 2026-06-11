"""Regression: API threads must never crash iterating live tracker state."""

import threading

from hometwin.overlay import camera_overlay, map_overlay
from hometwin.simulate import build_simulation


def test_overlay_safe_under_concurrent_stepping():
    tracker, state = build_simulation(seed=1)
    for _ in range(10):
        state.tick()
        tracker.step()

    errors = []
    stop = threading.Event()

    def hammer():
        while not stop.is_set():
            try:
                map_overlay(tracker)
                camera_overlay(tracker, "cam-kitchen")
                tracker.snapshot()
            except Exception as e:  # dict/deque resize races raise here
                errors.append(e)
                return

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for th in threads:
        th.start()
    for _ in range(120):
        state.tick()
        tracker.step()
    stop.set()
    for th in threads:
        th.join(timeout=5)
    assert not errors, errors[:3]
