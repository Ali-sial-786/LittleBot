"""The robot's screen shows a red REC sign with the time while it records a video (littlebot/capture), over cards
too, and nothing once the recording ends."""

from __future__ import annotations

import time
from typing import Any

import pytest
from face_stubs import StubRobot

from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")


def _loop() -> tuple[D.DisplayLoop, Any]:
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0})
    ctx.face = FaceController(ctx, seed=3)
    robot = StubRobot(ctx, None)
    return D.DisplayLoop(robot, max_frames=1), robot


def _frame(loop: D.DisplayLoop) -> Any:
    loop.max_frames = loop.frames + 1
    loop.run()
    return loop.window.copy()


def _reds(surf: Any, rect: Any) -> int:
    return sum(1 for x in range(rect.left, rect.right) for y in range(rect.top, rect.bottom)
               if (c := surf.get_at((x, y)))[0] > 200 and c[1] < 90 and c[2] < 90)


def _recording(robot: Any, active: bool, started: float | None = None) -> None:
    robot.ctx.bus.publish(D.REC_TOPIC, {"active": active, "id": "20261106-143000",
                                        "started_at": time.time() if started is None else started},
                          source="capture")


def test_rec_shows_while_recording_with_the_time() -> None:
    loop, robot = _loop()
    loop.open()
    try:
        before = _frame(loop)
        assert loop.recording_since is None and loop.rec_badge_rect() is None
        _recording(robot, True, started=time.time() - 65)
        assert loop.recording_since is not None
        shown = None
        for _ in range(4):  # (the dot blinks: one of a few frames has it lit)
            shown = _frame(loop)
            rect = loop.rec_badge_rect()
            if _reds(shown, rect) > 10:
                break
        rect = loop.rec_badge_rect()
        assert rect is not None and abs(rect.centerx - loop.face_rect.centerx) <= 1 and rect.top < 30
        assert loop._rec_label[0] == "REC 1:05"  # noqa: SLF001
        assert _reds(shown, rect) > 10 and _reds(before, rect) == 0
        _recording(robot, False)
        gone = _frame(loop)
        assert loop.recording_since is None and _reds(gone, rect) == 0
    finally:
        loop.close()
        robot.ctx.face.stop_ticker()
        robot.ctx.db.close()


def test_a_recording_already_going_when_the_screen_opens() -> None:
    loop, robot = _loop()
    robot.ctx.capture = type("C", (), {"recording": {"id": "x", "seconds": 30.0}})()
    loop.open()
    try:
        assert loop.recording_since is not None and time.monotonic() - loop.recording_since == pytest.approx(30, abs=2)
    finally:
        loop.close()
        robot.ctx.face.stop_ticker()
        robot.ctx.db.close()
