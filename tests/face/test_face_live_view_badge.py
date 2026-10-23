"""The robot's screen shows a small camera sign while a parent watches the camera live (vision.live_view)."""

from __future__ import annotations

from typing import Any

import pytest
from face_stubs import StubRobot, StubVision

from littlebot.core.events import Topics
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")


def _loop(**ctx_overrides: Any) -> tuple[D.DisplayLoop, Any]:
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0},
                            **ctx_overrides)
    ctx.face = FaceController(ctx, seed=3)
    robot = StubRobot(ctx, None)
    loop = D.DisplayLoop(robot, max_frames=1)
    return loop, robot


def _close(loop: D.DisplayLoop, robot: Any) -> None:
    loop.close()
    robot.ctx.face.stop_ticker()
    robot.ctx.db.close()


def _frame(loop: D.DisplayLoop) -> Any:
    loop.max_frames = loop.frames + 1
    loop.run()
    return loop.window.copy()


def _changed(a: Any, b: Any, rect: Any) -> int:
    return sum(1 for x in range(rect.left, rect.right) for y in range(rect.top, rect.bottom)
               if a.get_at((x, y))[:3] != b.get_at((x, y))[:3])


def _publish(robot: Any, viewers: int) -> None:
    robot.ctx.bus.publish(Topics.VISION_LIVE_VIEW, {"active": viewers > 0, "viewers": viewers}, source="vision")


def test_camera_sign_shows_only_while_someone_watches():
    loop, robot = _loop()
    loop.open()
    try:
        before = _frame(loop)
        rect = loop.live_badge_rect()
        assert loop.live_viewers == 0
        _publish(robot, 1)
        assert loop.live_viewers == 1
        watched = _frame(loop)
        assert _changed(before, watched, rect) > rect.w * rect.h // 5  # the sign is drawn there
        assert _changed(before, watched, pygame.Rect(0, 0, 100, 60)) == 0  # the opposite corner is untouched
        _publish(robot, 2)
        assert loop.live_viewers == 2
        _publish(robot, 0)
        gone = _frame(loop)
        assert loop.live_viewers == 0 and _changed(before, gone, rect) == 0  # removed when nobody watches
    finally:
        _close(loop, robot)


def test_camera_sign_is_small_and_in_the_top_right_corner():
    loop, robot = _loop()
    loop.open()
    try:
        _publish(robot, 1)
        _frame(loop)
        r, face = loop.live_badge_rect(), loop.face_rect
        assert r.w <= face.w * 0.1 and r.h <= face.h * 0.1  # subtle
        assert r.right <= face.right and r.top >= face.top
        assert r.left > face.centerx + face.w // 3 and r.bottom < face.top + face.h // 5  # out of the eyes' way
    finally:
        _close(loop, robot)


def test_camera_sign_scales_with_the_window():
    loop, robot = _loop()
    loop.open()
    try:
        small = loop.live_badge_rect().w
    finally:
        _close(loop, robot)
    ctx_display = {"headless": False, "show_sim_panel": False, "scale": 2.0}
    ctx = make_test_context(fakes=False, display=ctx_display)
    ctx.face = FaceController(ctx, seed=3)
    robot = StubRobot(ctx, None)
    loop = D.DisplayLoop(robot, max_frames=1)
    loop.open()
    try:
        assert loop.live_badge_rect().w == pytest.approx(2 * small, abs=2)
    finally:
        _close(loop, robot)


def test_camera_sign_matches_a_dark_face_too():
    loop, robot = _loop(face={"background_color": (11, 17, 28)})
    loop.open()
    try:
        before = _frame(loop)
        _publish(robot, 1)
        watched = _frame(loop)
        rect = loop.live_badge_rect()
        assert _changed(before, watched, rect) > 0
        brightest = max(sum(watched.get_at((x, y))[:3]) for x in range(rect.left, rect.right)
                        for y in range(rect.top, rect.bottom))
        assert brightest > sum((11, 17, 28)) + 120  # glows (eye colour) on the dark screen
    finally:
        _close(loop, robot)


def test_someone_already_watching_when_the_window_opens():
    class WatchedVision(StubVision):
        def status(self) -> dict[str, Any]:
            return {**super().status(), "live_view": {"viewers": 1}}

    loop, robot = _loop()
    robot.ctx.vision = WatchedVision()
    loop.open()
    try:
        assert loop.live_viewers == 1
    finally:
        _close(loop, robot)
    assert loop._live_sub is None  # unsubscribed on close


def test_drawn_over_an_on_screen_game_too():
    class Game:
        active = True

        def render(self, surface: Any, rect: Any, dt: float) -> bool:
            surface.fill((20, 120, 60), rect)
            return True

    loop, robot = _loop()
    robot.ctx.games = Game()
    loop.open()
    try:
        before = _frame(loop)
        _publish(robot, 1)
        watched = _frame(loop)
        assert _changed(before, watched, loop.live_badge_rect()) > 0
    finally:
        _close(loop, robot)
