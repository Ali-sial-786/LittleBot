"""The Camera & drive page's endpoints: hold-to-drive, stop, a set distance, a turn, refusals."""

from __future__ import annotations

import asyncio

from littlebot.core.clock import ManualClock
from littlebot.core.config import DriveConfig
from littlebot.motion.drive import MockDrive


async def settle(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def wheels(dctx, **kw) -> MockDrive:
    d = MockDrive(DriveConfig(enabled=True, max_linear_mps=0.2, max_angular_dps=80, **kw), clock=ManualClock())
    dctx.motion.drive = d
    return d


async def test_status_and_hold_to_drive(authed, dctx):
    d = wheels(dctx)
    info = (await authed.get("/api/motion/drive")).json()
    assert info["drive"]["enabled"] is True and info["refusal"] is None
    r = await authed.post("/api/motion/drive", json={"linear": 0.5, "angular": 0.0})
    assert r.status_code == 200 and r.json()["linear_mps"] == 0.1
    await settle()
    assert d.status()["moving"] is True
    r = await authed.post("/api/motion/drive", json={"linear": 0, "angular": 0})
    assert r.json()["stopped"] is True
    assert (await authed.post("/api/motion/drive/stop")).status_code == 200


async def test_move_and_turn(authed, dctx):
    d = wheels(dctx)
    assert (await authed.post("/api/motion/drive/move", json={"meters": 0.5})).status_code == 200
    await settle()
    assert d.status()["moving"] is True
    assert (await authed.post("/api/motion/drive/turn", json={"degrees": -90})).status_code == 200
    assert (await authed.post("/api/motion/drive/move", json={"meters": 5})).status_code == 422
    assert (await authed.post("/api/motion/drive", json={"linear": 2})).status_code == 422


async def test_refused_when_disabled(authed, dctx):
    dctx.motion.drive = MockDrive(DriveConfig(enabled=False), clock=ManualClock())
    r = await authed.post("/api/motion/drive", json={"linear": 0.5})
    assert r.status_code == 409 and "disabled" in r.json()["detail"]
    r = await authed.post("/api/motion/drive/move", json={"meters": 0.3})
    assert r.status_code == 409
    assert (await authed.post("/api/motion/drive/stop")).status_code == 200  # stopping is never refused
