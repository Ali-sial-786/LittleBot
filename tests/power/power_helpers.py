"""Helpers for the power service tests: a fake clock, a fake robot facade and a fake live voice."""

from __future__ import annotations

import asyncio
from typing import Any

from littlebot.core.events import Topics
from littlebot.power.monitor import cell_for_percent
from littlebot.power.protocol import Reading
from littlebot.power.service import PowerService
from littlebot.testing import make_test_context


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeRobot:
    """``ctx.extras["robot"]``: records STOP ALL and the power-off request (never powers anything off)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.stop_alls: list[str] = []
        self.poweroffs: list[str] = []
        ctx.extras["robot"] = self

    async def stop_all(self, source: str = "parent") -> dict[str, Any]:
        self.stop_alls.append(source)
        self.ctx.bus.publish(Topics.STOP_ALL, {"source": source})
        await asyncio.sleep(0)
        return {}

    def request_poweroff(self, reason: str) -> None:
        self.poweroffs.append(reason)


class FakeLive:
    """``ctx.live``: a call that is on, hears power notes and (``ends_on_goodbye``) hangs up after its goodbye."""

    def __init__(self, ctx: Any, *, ends_on_goodbye: bool = True) -> None:
        self.ctx = ctx
        self.active = True
        self.notes: list[tuple[str, bool]] = []
        self.stopped: list[str] = []
        self.ends_on_goodbye = ends_on_goodbye
        ctx.bus.subscribe(Topics.POWER_NOTE, self._on_note)
        ctx.live = self

    def _on_note(self, event: Any) -> None:
        self.notes.append((str(event.get("text")), bool(event.get("goodbye"))))
        if event.get("goodbye") and self.ends_on_goodbye:
            asyncio.get_running_loop().call_later(0.05, self._end, "battery")

    def _end(self, reason: str) -> None:
        if self.active:
            self.active = False
            self.ctx.bus.publish("live.call_ended", {"reason": reason}, source="live")

    def status(self) -> dict[str, Any]:
        return {"active": self.active}

    async def stop_session(self, reason: str = "stopped") -> dict[str, Any]:
        self.stopped.append(reason)
        self._end(reason)
        return {"ok": True}


def reading(percent: float | None = 50, *, charging: bool | None = False, charged: bool | None = False,
            t: float | None = 25.0, sag: float = 0.0, cell2: float | None = None) -> Reading:
    """A monitor reading for a pack at ``percent`` (both cells the same unless ``cell2``), ``sag`` V per cell lower."""
    if percent is None:
        return Reading(voltage=None, cell1=None, temperature_c=t, charging=charging, charged=charged, uptime_s=1, fw=1)
    c1 = cell_for_percent(percent) - sag + (0.1 if charging else 0.0)
    c2 = c1 if cell2 is None else cell2
    return Reading(voltage=round(c1 + c2, 3), cell1=round(c1, 3), temperature_c=t, charging=charging,
                   charged=charged, uptime_s=1, fw=1)


def make(**power: Any) -> tuple[Any, PowerService, Clock]:
    """A test context and a PowerService fed by hand (``feed``), on a fake clock. Never powers anything off."""
    ctx = make_test_context(power={"monitor": "none", **power})  # readings come from feed(), not a port
    clock = Clock()
    svc = PowerService(ctx, now=clock)  # without a FakeRobot it only logs: nothing here ever powers off
    svc.gauge.tau_s = 0.5  # these tests are about what happens, not the smoothing (test_power_battery has that)
    return ctx, svc, clock


def feed(svc: PowerService, clock: Clock, r: Reading, seconds: float, step: float = 1.0) -> list[str]:
    """``seconds`` of the same reading, once per ``step``; returns the alarms fired."""
    fired: list[str] = []
    end = clock.t + seconds
    while clock.t <= end + 1e-9:
        fired += svc.process(r)
        svc.drain()
        clock.t += step
    return fired


def topics(ctx: Any, pattern: str = "power.*") -> list[str]:
    return [e.topic for e in ctx.bus.recent(300, pattern)]


def events(ctx: Any, topic: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in ctx.bus.recent(300, topic)]
