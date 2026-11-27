"""While the always-recording buffer runs (littlebot/capture/buffer.py) the screen shows a small, still, soft red
dot at the top centre, where the REC sign goes: much smaller than REC, no words, and the REC sign takes its place
during an asked-for recording."""

from __future__ import annotations

import time
from typing import Any

import pytest
from face_stubs import StubRobot

from littlebot.capture.buffer import BUFFERING
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")


def _loop(capture: Any = None) -> tuple[D.DisplayLoop, Any]:
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0})
    ctx.face = FaceController(ctx, seed=3)
    ctx.capture = capture
    robot = StubRobot(ctx, None)
    return D.DisplayLoop(robot, max_frames=1), robot


def _frame(loop: D.DisplayLoop) -> Any:
    loop.max_frames = loop.frames + 1
    loop.run()
    return loop.window.copy()


def _reds(surf: Any, rect: Any) -> int:
    return sum(1 for x in range(rect.left, rect.right) for y in range(rect.top, rect.bottom)
               if (c := surf.get_at((x, y)))[0] > 180 and c[1] < 130 and c[2] < 140 and c[0] - c[1] > 80)


def _close(loop: D.DisplayLoop, robot: Any) -> None:
    loop.close()
    robot.ctx.face.stop_ticker()
    robot.ctx.db.close()


def test_the_topic_is_the_buffers() -> None:
    assert D.BUFFER_TOPIC == BUFFERING == "capture.buffering"


def test_a_small_steady_dot_while_buffering() -> None:
    loop, robot = _loop()
    loop.open()
    try:
        before = _frame(loop)
        assert loop.buffering is False and loop.buffer_dot_rect() is None
        robot.ctx.bus.publish(BUFFERING, {"active": True, "sound": True}, source="capture")
        assert loop.buffering is True
        rect = loop.buffer_dot_rect()
        assert rect is not None and abs(rect.centerx - loop.face_rect.centerx) <= 1 and rect.top < 30
        assert rect.w == rect.h <= 16  # small: a dot, not a sign
        frames = [_frame(loop) for _ in range(3)]
        assert all(_reds(f, rect) >= 6 for f in frames) and _reds(before, rect) == 0  # steady: it never blinks
        robot.ctx.bus.publish(BUFFERING, {"active": False, "sound": False}, source="capture")
        gone = _frame(loop)
        assert loop.buffering is False and loop.buffer_dot_rect() is None and _reds(gone, rect) == 0
    finally:
        _close(loop, robot)


def test_the_rec_sign_wins_during_a_recording() -> None:
    loop, robot = _loop()
    loop.open()
    try:
        robot.ctx.bus.publish(BUFFERING, {"active": True, "sound": False}, source="capture")
        _frame(loop)
        dot = loop.buffer_dot_rect()
        robot.ctx.bus.publish(D.REC_TOPIC, {"active": True, "id": "x", "started_at": time.time()}, source="capture")
        _frame(loop)
        assert loop.buffer_dot_rect() is None and loop.rec_badge_rect() is not None
        assert loop.rec_badge_rect().w > 3 * dot.w  # (the REC sign is the big one)
        robot.ctx.bus.publish(D.REC_TOPIC, {"active": False, "id": "x"}, source="capture")
        assert loop.buffer_dot_rect() == dot  # still buffering (as far as the screen was told): the dot is back
    finally:
        _close(loop, robot)


def test_buffering_already_when_the_screen_opens() -> None:
    loop, robot = _loop(type("C", (), {"recording": None, "buffering": True})())
    loop.open()
    try:
        assert loop.buffering is True and loop.buffer_dot_rect() is not None
    finally:
        _close(loop, robot)
