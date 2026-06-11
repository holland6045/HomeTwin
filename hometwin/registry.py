"""Plugin registry: the extension seam for the whole system.

Sensors, detectors, frame sources, and trainers register under a string kind
and name. Config files reference plugins by name, so adding hardware support
is: write a class, register it, name it in YAML. Nothing in the core changes.

Third-party packages can self-register via the `hometwin.plugins`
entry-point group.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from importlib.metadata import entry_points

_REGISTRY: dict[str, dict[str, Callable]] = {}

KINDS = ("sensor", "detector", "frame_source", "trainer")


def register(kind: str, name: str) -> Callable:
    if kind not in KINDS:
        raise ValueError(f"unknown plugin kind {kind!r}, expected one of {KINDS}")

    def deco(factory: Callable) -> Callable:
        _REGISTRY.setdefault(kind, {})[name] = factory
        return factory

    return deco


def create(kind: str, name: str, **kwargs):
    factory = _REGISTRY.get(kind, {}).get(name)
    if factory is None:
        raise KeyError(
            f"no {kind} plugin named {name!r}; available: {sorted(_REGISTRY.get(kind, {}))}"
        )
    return factory(**kwargs)


def available(kind: str) -> list[str]:
    return sorted(_REGISTRY.get(kind, {}))


_BUILTIN_MODULES = (
    "hometwin.sensors.mock",
    "hometwin.sensors.camera",
    "hometwin.sensors.ble",
    "hometwin.sensors.tomography",
    "hometwin.sensors.network",
    "hometwin.detectors.aruco",
    "hometwin.detectors.onnx",
    "hometwin.training.trainers",
)

_loaded = False


def load_plugins() -> None:
    """Import builtin plugin modules and any installed entry-point plugins.

    Builtin modules guard optional hardware deps internally: a module whose
    backend library is missing still registers, but its factory raises a
    clear error only if the config actually selects it.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    for mod in _BUILTIN_MODULES:
        import_module(mod)
    for ep in entry_points(group="hometwin.plugins"):
        ep.load()
