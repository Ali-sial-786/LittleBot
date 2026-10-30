"""Shared helpers for the motion tests (imported by conftest.py and test modules)."""

from __future__ import annotations

import asyncio
import importlib.abc
from pathlib import Path
from typing import Any, Callable

import yaml

from littlebot.motion.controller import MotionController

FULL_CALIBRATION = {
    "version": 1,
    "servos": {
        "head": {"channel": 0, "neutral": 92.0, "min_angle": 55.0, "max_angle": 125.0},
        "left_arm": {"channel": 1, "neutral": 22.0, "min_angle": 12.0, "max_angle": 140.0},
        "right_arm": {"channel": 2, "neutral": 158.0, "min_angle": 40.0, "max_angle": 168.0, "inverted": True,
                      "positive_toward": "min"},
        "head_tilt": {"channel": 3, "neutral": 88.0, "min_angle": 72.0, "max_angle": 108.0},
    },
}
DEFAULT_JOINTS = {"head", "head_tilt", "left_arm", "right_arm"}


def write_calibration(path: Path, data: dict[str, Any] | None = None) -> Path:
    path.write_text(yaml.safe_dump(data if data is not None else FULL_CALIBRATION), encoding="utf-8")
    return path


class Smbus2Blocker(importlib.abc.MetaPathFinder):
    """Fails any import of smbus2 and records the attempt."""

    def __init__(self) -> None:
        self.attempts = 0

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname == "smbus2" or fullname.startswith("smbus2."):
            self.attempts += 1
            raise ImportError("smbus2 blocked by test")
        return None


async def run_until(predicate: Callable[[], bool], max_iterations: int = 20000) -> None:
    """Yield to the loop until predicate() holds (simulated time advances via ManualClock)."""
    for _ in range(max_iterations):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def record_writes(controller: MotionController) -> list[tuple[float, int, float]]:
    """Wrap the controller's driver so every pulse write is recorded as (clock time, channel, pulse)."""
    log: list[tuple[float, int, float]] = []
    driver = controller.driver
    original = driver.set_pulse_us

    def _rec(channel: int, pulse_us: float) -> None:
        log.append((controller._clock.now(), channel, pulse_us))  # noqa: SLF001
        original(channel, pulse_us)

    driver.set_pulse_us = _rec  # type: ignore[method-assign]
    return log
