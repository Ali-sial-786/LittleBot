"""Fixtures for the motion tests: fast simulated time, isolated calibration paths."""

from __future__ import annotations

import sys
from typing import Any, Callable

import pytest

from littlebot.core.clock import ManualClock
from littlebot.motion.controller import MotionController
from littlebot.testing import make_test_context
from motion_testkit import Smbus2Blocker


@pytest.fixture
def smbus2_blocker():
    blocker = Smbus2Blocker()
    saved = sys.modules.pop("smbus2", None)
    sys.meta_path.insert(0, blocker)
    try:
        yield blocker
    finally:
        sys.meta_path.remove(blocker)
        if saved is not None:
            sys.modules["smbus2"] = saved


@pytest.fixture
def make_ctx(tmp_path):
    """make_ctx(**motion_overrides) -> RobotContext (calibration file defaults to a missing tmp path)."""
    made = []

    def _make(**motion: Any):
        motion.setdefault("calibration_file", str(tmp_path / "no-calibration.yaml"))
        ctx = make_test_context(motion=motion)
        made.append(ctx)
        return ctx

    yield _make
    for c in made:
        c.db.close()


@pytest.fixture
async def make_controller(make_ctx):
    """await make_controller(ctx=None, clock=None, start=True, driver_factory=None, **motion) -> MotionController."""
    controllers: list[MotionController] = []

    async def _make(ctx: Any = None, *, clock: Any = None, start: bool = True,
                    driver_factory: Callable[..., Any] | None = None, **motion: Any) -> MotionController:
        ctx = ctx or make_ctx(**motion)
        c = MotionController(ctx, clock=clock or ManualClock(), driver_factory=driver_factory)
        if start:
            await c.start()
        controllers.append(c)
        return c

    yield _make
    for c in controllers:
        await c.stop()
