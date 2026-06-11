"""Apartment item tracker: modular CV + sensor fusion for locating tagged items.

Core is pure stdlib + PyYAML so it runs on anything from a laptop to a Pi.
Hardware-specific backends (OpenCV cameras, ONNX detectors, BLE radios,
MCU sensor nodes) plug in through the registry in `hometwin.registry`.
"""

__version__ = "0.1.0"
