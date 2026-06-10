"""Persistent state: last-known item locations survive tracker restarts.

The point of the system is "where did I leave it" — an estimate from before
a reboot is still the best answer until a sensor says otherwise. Saved
tracks restore as-is with their original timestamps; staleness reporting
and covariance growth on the next prediction handle the elapsed time
naturally.

Writes are atomic (tmp + rename) so a crash mid-save never corrupts the
last good state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, state: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "tracks": state}, f)
        os.replace(tmp, self.path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("tracks", [])
        except (json.JSONDecodeError, OSError):
            return []  # corrupt state is treated as no state
