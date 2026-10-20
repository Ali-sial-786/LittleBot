"""Time source abstraction so timed behaviour (activities, animations) is testable."""

from __future__ import annotations

import asyncio
import time


class Clock:
    """Real clock. ``scale`` < 1 speeds up scripted waits (used by tests and demos)."""

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = max(0.0, float(scale))

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) * self.scale)

    def scaled(self, seconds: float) -> float:
        return max(0.0, seconds) * self.scale


class ManualClock(Clock):
    """Clock whose ``now`` only advances when told to (for pure-logic unit tests)."""

    def __init__(self, start: float = 0.0) -> None:
        super().__init__(scale=0.0)
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += seconds
        return self._t

    async def sleep(self, seconds: float) -> None:
        self._t += max(0.0, seconds)
        await asyncio.sleep(0)
