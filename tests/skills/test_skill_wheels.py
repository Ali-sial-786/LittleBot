"""Driving by voice: the Wheels skill starts moves on ctx.motion.drive and says why when it can't."""

from __future__ import annotations

import asyncio
import types

from littlebot.core.clock import ManualClock
from littlebot.core.config import DriveConfig
from littlebot.motion.drive import MockDrive
from littlebot.skills.wheels import Wheels


class FakeDrive:
    def __init__(self, refusal: str | None = None, enabled: bool = True) -> None:
        self.cfg = DriveConfig(enabled=enabled)
        self._refusal = refusal
        self.calls: list[tuple[str, float]] = []
        self.stops = 0

    def refusal(self):
        return self._refusal

    async def forward(self, meters):
        self.calls.append(("forward", meters))
        return True

    async def turn(self, degrees):
        self.calls.append(("turn", degrees))
        return True

    def stop(self):
        self.stops += 1

    def status(self):
        return {"moving": False}


def skill(drive) -> Wheels:
    return Wheels(types.SimpleNamespace(motion=types.SimpleNamespace(drive=drive)))


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


async def test_drive_and_turn_start_the_move():
    d = FakeDrive()
    s = skill(d)
    assert s.available() and {t["name"] for t in s.tools()} == {"drive_distance", "turn", "stop_moving"}
    assert (await s.run("drive_distance", {"centimetres": 50})).startswith("ok:")
    assert (await s.run("drive_distance", {"centimetres": 20, "direction": "backward"})).startswith("ok:")
    assert (await s.run("turn", {"direction": "right"})).startswith("ok:")
    assert (await s.run("turn", {"degrees": 180, "direction": "left"})).startswith("ok:")
    await settle()
    assert d.calls == [("forward", 0.5), ("forward", -0.2), ("turn", -90.0), ("turn", 180.0)]


async def test_limits_and_defaults():
    d = FakeDrive()
    s = skill(d)
    await s.run("drive_distance", {"centimetres": 9999})
    await s.run("drive_distance", {})
    await s.run("turn", {"degrees": 5000, "direction": "left"})
    await settle()
    assert d.calls == [("forward", 2.0), ("forward", 0.3), ("turn", 360.0)]


async def test_locked_wheels_say_why_and_stop_always_works():
    d = FakeDrive(refusal="physical motion is not armed")
    s = skill(d)
    res = await s.run("drive_distance", {"centimetres": 30})
    assert res.startswith("error:") and "dashboard" in res and not d.calls
    assert (await s.run("stop_moving", {})).startswith("ok:") and d.stops == 1


def test_hidden_without_enabled_wheels():
    assert not skill(FakeDrive(enabled=False)).available()
    assert not Wheels(types.SimpleNamespace(motion=None)).available()


async def test_with_the_real_simulated_drive():
    clock = ManualClock()
    d = MockDrive(DriveConfig(enabled=True), clock=clock)
    s = skill(d)
    assert (await s.run("turn", {"degrees": 90, "direction": "left"})).startswith("ok:")
    await settle()
    assert d.status()["moving"] is True
    await s.run("stop_moving", {})
