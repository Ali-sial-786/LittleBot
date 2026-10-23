"""Helpers for the games tests (fake clock, stepping, stub robot for the display loop)."""

from __future__ import annotations

import threading
from typing import Any


class FakeClock:
    """Monotonic clock the tests move by hand."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def run(game: Any, seconds: float, dt: float = 1 / 30, clock: FakeClock | None = None) -> None:
    """Advance a game by ``seconds`` in fixed frames (moving the fake clock along)."""
    for _ in range(int(round(seconds / dt))):
        if clock is not None:
            clock.advance(dt)
        game.update(dt)


def distinct_colors(surface: Any, step: int = 24) -> int:
    """How many different colours a coarse sample of the surface has (blank screens have ~1)."""
    w, h = surface.get_size()
    return len({tuple(surface.get_at((x, y)))[:3] for x in range(0, w, step) for y in range(0, h, step)})


class StubRobot:
    """What face.display.DisplayLoop needs from app.robot.LittleBot."""

    def __init__(self, ctx: Any, runtime: Any = None) -> None:
        self.ctx = ctx
        self.runtime = runtime
        self.shutdown_event = threading.Event()
        self.stop_all_calls: list[str] = []

    def status(self) -> dict[str, Any]:
        return {"name": "LittleBot", "state": self.ctx.state.snapshot()}

    async def stop_all(self, source: str = "parent") -> dict[str, Any]:
        self.stop_all_calls.append(source)
        return {}

    def request_shutdown(self, reason: str = "requested") -> None:
        self.shutdown_event.set()
