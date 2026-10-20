"""Platform detection (Raspberry Pi vs desktop) without importing hardware libraries."""

from __future__ import annotations

import functools
import platform
from pathlib import Path


@functools.lru_cache(maxsize=1)
def pi_model() -> str | None:
    """Return e.g. 'Raspberry Pi 5 Model B Rev 1.0', or None when not on a Pi."""
    for p in (Path("/proc/device-tree/model"), Path("/sys/firmware/devicetree/base/model")):
        try:
            text = p.read_bytes().decode("utf-8", "ignore").strip("\x00\n ")
        except OSError:
            continue
        if "raspberry pi" in text.lower():
            return text
    return None


def is_raspberry_pi() -> bool:
    return pi_model() is not None


def is_pi5() -> bool:
    model = pi_model() or ""
    return "Raspberry Pi 5" in model


def describe_platform() -> dict[str, str | bool | None]:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "pi_model": pi_model(),
        "is_raspberry_pi": is_raspberry_pi(),
    }
