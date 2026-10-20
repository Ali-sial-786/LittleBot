"""Composition root + behaviour glue tests (with fakes, no real subsystems)."""

from __future__ import annotations

import asyncio

from littlebot.app.behaviours import Behaviours
from littlebot.app.main import build_parser, overrides_from_args
from littlebot.app.robot import LittleBot
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.testing import make_test_context


def _robot(**cfg):
    ctx = make_test_context(dashboard={"enabled": False}, **cfg)
    bot = LittleBot(ctx)
    bot.build_subsystems(only=set())  # glue only; fakes are pre-injected on ctx
    return bot


def test_cli_overrides():
    args = build_parser().parse_args(["--headless", "--lan", "--port", "9001", "--debug", "--scale", "1.5"])
    o = overrides_from_args(args)
    assert o["display"] == {"headless": True, "show_debug": True, "scale": 1.5}
    assert o["dashboard"] == {"port": 9001, "lan_access": True}
    assert o["logging"] == {"level": "DEBUG"}


def test_robot_start_stop_lifecycle():
    bot = _robot()
    bot.start()
    try:
        assert bot.runtime.running
        st = bot.status()
        assert st["state"]["state"] == "IDLE"
        assert st["mode"] == "simulation"
        assert st["face"]["expression"] == "neutral"
    finally:
        bot.shutdown("test")
    assert not bot.runtime.running
    # Motion made safe on shutdown.
    assert bot.ctx.motion.estop is True


def test_state_changes_drive_face():
    bot = _robot()
    bot.start()
    try:
        ctx = bot.ctx
        ctx.state.transition(RobotState.LISTENING, "test")
        assert ctx.face.state == "listening"
        ctx.state.transition(RobotState.SLEEPING, "test", force=True)
        assert ctx.face.state == "sleeping" and ctx.face.expression == "sleepy"
        ctx.state.transition(RobotState.IDLE, "wake")
        assert ctx.face.state == "idle"
        assert ("open_eyes", (), {}) in ctx.face.calls
    finally:
        bot.shutdown("test")


def test_camera_faces_drive_gaze_and_presence_wakes():
    bot = _robot()
    bot.start()
    try:
        ctx = bot.ctx
        ctx.bus.publish(Topics.VISION_FACES, {"faces": [
            {"x": 0, "y": 0, "w": 10, "h": 10, "gaze_x": 0.2, "gaze_y": 0.4},
            {"x": 0, "y": 0, "w": 50, "h": 50, "gaze_x": 0.8, "gaze_y": 0.6},
        ]})
        assert ctx.face.gaze == (0.8, 0.6)  # follows the biggest (closest) face
        ctx.settings.set("vision.follow_faces", False)
        ctx.bus.publish(Topics.VISION_FACES, {"faces": [{"w": 5, "h": 5, "gaze_x": 0.1, "gaze_y": 0.1}]})
        assert ctx.face.gaze == (0.8, 0.6)
        bot.sleep()
        assert ctx.state.state == RobotState.SLEEPING
        ctx.bus.publish(Topics.VISION_PERSON_PRESENT, {"gaze_x": 0.5, "gaze_y": 0.5})
        assert ctx.state.state == RobotState.IDLE
    finally:
        bot.shutdown("test")


def test_stop_all_calms_everything():
    bot = _robot()
    bot.start()
    try:
        ctx = bot.ctx
        lease = ctx.arbiter.acquire("something", ["face"], 50)
        ctx.state.transition(RobotState.PLAYING_ACTIVITY, "test")
        bot.runtime.run(bot.stop_all("test"))
        assert ctx.state.state == RobotState.IDLE
        assert lease is not None and not lease.active
        assert ("stop_output", (), {}) in ctx.audio.calls
        assert ("cancel", (), {}) in ctx.motion.calls
        assert any(e.topic == Topics.STOP_ALL for e in ctx.bus.recent(50))
    finally:
        bot.shutdown("test")


def test_missing_subsystems_do_not_crash_status():
    ctx = make_test_context(fakes=False, dashboard={"enabled": False})
    from littlebot.testing import FakeFace

    ctx.face = FakeFace(ctx.bus)
    bot = LittleBot(ctx)
    bot.build_subsystems(only={"face"})
    st = bot.status()
    for name in ("audio", "vision", "motion", "media", "brain", "activities"):
        assert st[name] == {"status": "unavailable"}
    bot.runtime.start()
    bot.runtime.run(bot.stop_all("test"))
    bot.runtime.stop()


async def test_idle_watch_gets_sleepy(monkeypatch):
    ctx = make_test_context()
    ctx.settings.set("system.sleep_after_idle_minutes", 0.0001)
    b = Behaviours(ctx)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        sleeps.append(s)
        await real_sleep(0.01)

    monkeypatch.setattr("littlebot.app.behaviours.asyncio.sleep", fast_sleep)
    await b.start()
    try:
        for _ in range(100):
            if ctx.state.state == RobotState.SLEEPING:
                break
            await real_sleep(0.01)
        assert ctx.state.state == RobotState.SLEEPING
    finally:
        await b.stop()
