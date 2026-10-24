"""The face's battery sign (littlebot/face/battery_badge.py): when it shows, its fade, and the display drawing it."""

from __future__ import annotations

from typing import Any

import pytest
from face_stubs import StubRobot

from littlebot.core.events import Topics
from littlebot.face import battery_badge as B
from littlebot.face.controller import FaceController
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")


class Clock:
    def __init__(self) -> None:
        self.t = 50.0

    def __call__(self) -> float:
        return self.t


def test_the_sign_shows_for_a_while_and_fades():
    clock = Clock()
    badge = B.BatteryBadge(clock)
    assert badge.current() is None and not badge.visible
    badge.show(57.4, True, 4)
    b = badge.current()
    assert b is not None and b.percent == 57 and b.charging and b.alpha == 0.0  # fading in
    clock.t += B.FADE_IN_SECONDS
    assert badge.current().alpha == 1.0
    clock.t += 3.0
    badge.show(58, True, 4)  # shown again while showing: no new fade-in, a longer stay
    assert badge.current().alpha == 1.0 and badge.current().percent == 58
    clock.t += 4.0 + B.FADE_SECONDS / 2
    assert 0.0 < badge.current().alpha < 1.0  # fading out
    clock.t += B.FADE_SECONDS
    assert badge.current() is None


def test_bad_values_from_the_bus_are_safe():
    clock = Clock()
    badge = B.BatteryBadge(clock)
    badge.show("lots", None, "forever")
    b = badge.current()
    assert b is not None and b.percent is None and b.charging is False
    badge.show(250, False, 1e9)
    assert badge.current().percent == 100
    clock.t += B.MAX_SECONDS + B.FADE_SECONDS + 1
    assert badge.current() is None
    badge.on_event({"percent": 12, "charged": True, "seconds": 2})
    assert badge.current().charging is True and badge.current().percent == 12
    badge.hide()
    assert badge.current() is None


def test_colours_follow_the_level():
    from littlebot.games import art

    assert B.level_colour(10) == art.STRAWBERRY and B.level_colour(35) == art.LEMON and B.level_colour(80) == art.MINT
    assert B.level_colour(None) == art.INK_SOFT


def test_drawn_small_in_the_top_left_corner():
    surf = pygame.Surface((720, 480))
    face = pygame.Rect(0, 0, 720, 480)
    surf.fill((0, 0, 0))
    for pct, charging in ((None, True), (5, False), (57, True), (100, False)):
        r = B.draw_badge(surf, face, B.Badge(pct, charging, 1.0))
        assert r.left < face.w // 4 and r.top < face.h // 6 and r.w <= face.w * 0.25 and r.h <= face.h * 0.15
    assert surf.get_at((r.centerx, r.centery))[:3] != (0, 0, 0)
    assert surf.get_at((face.w - 10, face.h - 10))[:3] == (0, 0, 0)  # nothing anywhere else


# ---------------------------------------------------------------- in the display loop
def _loop() -> tuple[Any, Any]:
    from littlebot.face import display as D

    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0})
    ctx.face = FaceController(ctx, seed=3)
    robot = StubRobot(ctx, None)
    return D.DisplayLoop(robot, max_frames=1), robot


def _frame(loop: Any) -> Any:
    loop.max_frames = loop.frames + 1
    loop.run()
    return loop.window.copy()


def _changed(a: Any, b: Any, rect: Any) -> int:
    return sum(1 for x in range(rect.left, rect.right) for y in range(rect.top, rect.bottom)
               if a.get_at((x, y))[:3] != b.get_at((x, y))[:3])


def test_the_display_draws_the_sign_on_power_show():
    loop, robot = _loop()
    loop.open()
    try:
        before = _frame(loop)
        x, y, w, h = B.badge_rect(loop.face_rect)
        rect = pygame.Rect(x, y, w, h)
        robot.ctx.bus.publish(Topics.POWER_SHOW, {"percent": 64, "charging": True, "seconds": 5}, source="power")
        assert loop.battery.visible
        loop.battery._shown = (64, True, 0.0, loop.battery._shown[3])  # skip the fade-in
        shown = _frame(loop)
        assert _changed(before, shown, rect) > rect.w * rect.h // 5
        loop.battery.hide()
        gone = _frame(loop)
        assert _changed(before, gone, rect) < rect.w * rect.h // 50  # gone again (the face itself may move a pixel)
    finally:
        loop.close()
        robot.ctx.face.stop_ticker()
        robot.ctx.db.close()
