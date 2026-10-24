"""MX Player-style edge swipes: the gesture, brightness (backlight / software dimming), the bar, the window."""

from __future__ import annotations

import logging
import os
import threading
import time

import pytest

from littlebot.core.events import EventBus
from littlebot.core.runtime import AsyncRuntime
from littlebot.core.settings import Settings, build_defaults
from littlebot.face import display as D
from littlebot.face import edge_controls as EC
from littlebot.face.controller import FaceController
from littlebot.testing import FakeAudio, make_test_context

pygame = pytest.importorskip("pygame")


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class Box:
    """A level held in memory, recording what was applied and the feedback given."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.applied: list[float] = []
        self.fed: list[tuple[float, bool]] = []

    def apply(self, v: float) -> None:
        self.value = v
        self.applied.append(v)

    def feedback(self, v: float, final: bool) -> None:
        self.fed.append((v, final))


def controls(volume: float = 0.4, brightness: float = 1.0):
    clock = Clock()
    vol, bright = Box(volume), Box(brightness)
    edges = EC.EdgeControls(
        left=EC.Level("brightness", lambda: bright.value, bright.apply, EC.BRIGHTNESS_FLOOR, 1.0),
        right=EC.Level("volume", lambda: vol.value, vol.apply, 0.0, 0.8, feedback=vol.feedback),
        clock=clock)
    return edges, vol, bright, clock


def sweep(fraction: float) -> float:
    """Normalised vertical distance for ``fraction`` of a full sweep."""
    return EC.SWEEP * fraction


# ------------------------------------------------------------------ the gesture
def test_vertical_drag_on_the_right_edge_sets_the_volume_proportionally():
    edges, vol, bright, clock = controls(volume=0.4)
    assert edges.press(0.95, 0.8) and edges.state == EC.PENDING
    assert edges.move(0.95, 0.8 - sweep(0.25)) == EC.ACTIVE  # a quarter sweep up: +25 % of the 0..0.8 range
    assert vol.applied == [pytest.approx(0.6)]
    clock.advance(0.2)
    edges.move(0.95, 0.8 + sweep(0.25))  # back down past the start: relative to where the finger started
    assert vol.value == pytest.approx(0.2)
    swipe = edges.release(0.95, 0.8 + sweep(0.25))
    assert swipe == EC.Swipe("volume", 0.4, pytest.approx(0.2))
    assert bright.applied == [] and edges.state == EC.IDLE


def test_left_edge_controls_the_brightness_down_to_its_floor():
    edges, vol, bright, clock = controls(brightness=1.0)
    assert edges.press(0.03, 0.2)
    edges.move(0.03, 0.2 + sweep(0.5))  # half a sweep down: 1.0 - 0.5 * 0.85
    assert bright.value == pytest.approx(0.575, abs=0.006)
    clock.advance(0.2)
    edges.move(0.03, 1.0)  # far down: never below the floor
    swipe = edges.release(0.03, 1.0)
    assert bright.value == EC.BRIGHTNESS_FLOOR and swipe.end == EC.BRIGHTNESS_FLOOR
    assert vol.applied == []


def test_volume_ceiling():
    edges, vol, _bright, _clock = controls(volume=0.7)
    edges.press(0.97, 0.9)
    edges.move(0.97, 0.0)
    assert edges.release(0.97, 0.0).end == 0.8 and vol.value == 0.8  # the configured maximum, never more


def test_horizontal_moves_centre_presses_and_taps_change_nothing():
    edges, vol, bright, _clock = controls()
    assert edges.press(0.95, 0.5)
    assert edges.move(0.80, 0.52) == EC.REJECTED  # sideways
    assert edges.move(0.80, 0.1) == EC.REJECTED  # and stays a plain touch
    assert edges.release(0.80, 0.1) is None
    assert edges.press(0.5, 0.5) is False and edges.move(0.5, 0.1) == EC.IDLE  # the middle of the face
    assert edges.release(0.5, 0.1) is None
    assert edges.press(0.05, 0.5)
    assert edges.move(0.05, 0.5 + 8 / 480) == EC.PENDING  # within the slop
    assert edges.release(0.05, 0.5 + 8 / 480) is None  # a tap
    assert vol.applied == [] and bright.applied == [] and edges.bar() is None


def test_edge_zone_width():
    edges, *_ = controls()
    assert edges.zone_at(EC.EDGE_ZONE - 0.01) == "left" and edges.zone_at(1 - EC.EDGE_ZONE + 0.01) == "right"
    assert edges.zone_at(EC.EDGE_ZONE + 0.02) is None and edges.zone_at(1 - EC.EDGE_ZONE - 0.02) is None


def test_applies_at_most_ten_times_a_second_and_blips_every_quarter_second():
    edges, vol, _bright, clock = controls(volume=0.0)
    edges.press(0.95, 0.9)
    y = 0.9
    for _ in range(30):  # one second of 30 fps moves
        y -= 0.01
        edges.move(0.95, y)
        clock.advance(1 / 30)
        edges.tick()
    assert 8 <= len(vol.applied) <= 11
    assert all(not final for _v, final in vol.fed) and 3 <= len(vol.fed) <= 5
    edges.move(0.95, y - 0.05)
    swipe = edges.release(0.95, y - 0.05)
    assert vol.applied[-1] == swipe.end == vol.value  # the final level is applied on release...
    assert vol.fed[-1] == (swipe.end, True)  # ...and heard


def test_the_bar_shows_while_sliding_and_three_seconds_after_then_fades():
    edges, _vol, _bright, clock = controls(volume=0.4)
    edges.press(0.95, 0.5)
    edges.move(0.95, 0.3)
    bar = edges.bar()
    assert bar.name == "volume" and bar.side == "right" and bar.alpha == 1.0
    assert bar.fill == pytest.approx(bar.value / 0.8)
    clock.advance(10)  # held still a long time: still shown
    assert edges.bar().alpha == 1.0
    edges.release(0.95, 0.3)
    clock.advance(2.9)
    assert edges.bar().alpha == 1.0
    clock.advance(0.25)
    assert 0 < edges.bar().alpha < 1
    clock.advance(0.2)
    assert edges.bar() is None


def test_changes_made_elsewhere_show_the_bar_but_our_own_echo_does_not():
    edges, *_rest, clock = controls()
    assert edges.show("volume", 0.3) and edges.bar().value == 0.3
    edges.nudge("volume", 0.1)
    assert edges.bar().value == pytest.approx(0.5)  # 0.4 + 0.1
    assert edges.show("volume", 0.5) is False  # the setting's echo of our own change
    clock.advance(EC.ECHO_SECONDS + 0.1)
    assert edges.show("volume", 0.65) and edges.bar().value == 0.65
    assert edges.show("nope", 1.0) is False


def test_a_late_echo_does_not_jump_the_bar_while_sliding():
    edges, *_rest = controls(volume=0.4)
    edges.press(0.95, 0.5)
    edges.move(0.95, 0.3)
    value = edges.bar().value
    assert edges.show("volume", 0.1) is False and edges.bar().value == value


def test_nudge_steps_within_the_range():
    edges, vol, _bright, _clock = controls(volume=0.75, brightness=0.2)
    assert edges.nudge("volume", EC.KEY_STEP) == EC.Swipe("volume", 0.75, 0.8)
    assert vol.fed == [(0.8, True)]
    assert edges.nudge("brightness", -EC.KEY_STEP).end == EC.BRIGHTNESS_FLOOR
    assert edges.nudge("contrast", 0.1) is None


def test_cancel_keeps_the_level_reached():
    edges, vol, _bright, _clock = controls(volume=0.4)
    edges.press(0.95, 0.5)
    edges.move(0.95, 0.5 - sweep(0.25))
    swipe = edges.cancel()
    assert swipe.end == pytest.approx(0.6) and vol.value == pytest.approx(0.6) and edges.state == EC.IDLE


# ------------------------------------------------------------------ brightness
def test_software_dimming_darkens_the_frame_with_one_cached_veil(pygame_display):
    b = EC.Brightness(1.0)
    surf = pygame.Surface((60, 40))
    surf.fill((200, 100, 50))
    assert b.mode == "software dimming" and b.apply(surf) is False  # 100 %: nothing to do
    assert b.set(0.4) == 0.4
    assert b.apply(surf)
    r, g, bl, *_ = surf.get_at((5, 5))
    assert (r, g, bl) == pytest.approx((80, 40, 20), abs=3)
    veil = b._veil
    b.apply(surf)
    assert b._veil is veil  # the same level: the same surface
    b.set(0.6)
    surf.fill((200, 100, 50))
    b.apply(surf)
    assert b._veil is not veil and surf.get_at((5, 5))[0] == pytest.approx(120, abs=3)
    assert b.set(0.0) == EC.BRIGHTNESS_FLOOR and b.set("x") == 1.0  # never black


def _fake_backlight(tmp_path, name="fake_bl", max_raw=255, value=255):
    d = tmp_path / name
    d.mkdir()
    (d / "max_brightness").write_text(f"{max_raw}\n")
    (d / "brightness").write_text(f"{value}\n")
    return d


def test_backlight_found_and_written(tmp_path):
    d = _fake_backlight(tmp_path)
    b = EC.Brightness(1.0, sysfs=tmp_path)
    assert b.mode == "backlight" and b.backlight.name == "fake_bl"
    assert b.set(0.5) == 0.5 and (d / "brightness").read_text().strip() == "128"
    assert b.backlight.read() == pytest.approx(128 / 255)
    b.set(0.0)
    assert (d / "brightness").read_text().strip() == str(round(EC.BRIGHTNESS_FLOOR * 255))
    surf = pygame.Surface((10, 10))
    assert b.apply(surf) is False  # the backlight dims: no software veil


def test_backlight_that_needs_root_falls_back_to_software(tmp_path, monkeypatch, caplog):
    _fake_backlight(tmp_path)
    real_access = os.access
    monkeypatch.setattr(EC.os, "access", lambda p, mode: False if mode == os.W_OK else real_access(p, mode))
    with caplog.at_level(logging.INFO, logger="littlebot.face.edge_controls"):
        b = EC.Brightness(1.0, sysfs=tmp_path)
    assert b.backlight is None and b.mode == "software dimming"
    assert "needs root" in caplog.text


def test_no_backlight_or_a_broken_one(tmp_path):
    assert EC.find_backlight(tmp_path / "missing") is None
    assert EC.find_backlight(None) is None
    (tmp_path / "odd").mkdir()  # no max_brightness
    assert EC.find_backlight(tmp_path) is None
    d = _fake_backlight(tmp_path, "zz_bl")
    b = EC.Brightness(1.0, sysfs=tmp_path)
    assert b.backlight is not None
    (d / "brightness").unlink()
    d.rename(tmp_path / "gone")  # writing now fails: software dimming from here on
    assert b.set(0.5) == 0.5 and b.mode == "software dimming"


# ------------------------------------------------------------------ the bar's look
def test_bar_sprites_draw_and_are_cached(pygame_display):
    surf = pygame.Surface((720, 480))
    rect = surf.get_rect()
    r = EC.draw_bar(surf, rect, EC.Bar("volume", "right", 0.55, 0.69, 1.0))
    assert r.right <= rect.right and r.centerx > rect.centerx
    left = EC.draw_bar(surf, rect, EC.Bar("brightness", "left", 0.4, 0.4, 0.5), shade=0.6)
    assert left.centerx < rect.centerx and left.w >= EC.BAR_W
    a = EC._bar_sprite("volume", 55, 138, 1.0)
    assert EC._bar_sprite("volume", 55, 138, 1.0) is a
    EC.draw_bar(surf, rect, EC.Bar("volume", "right", 0.0, 0.0, 1.0))  # muted: a cross, no fill


# ------------------------------------------------------------------ in the face window
class _Robot:
    def __init__(self, ctx, runtime):
        self.ctx = ctx
        self.runtime = runtime
        self.shutdown_event = threading.Event()

    def status(self):
        return {"name": "LittleBot", "state": self.ctx.state.snapshot()}

    async def stop_all(self, source="parent"):
        return {}

    def request_shutdown(self, reason="requested"):
        self.shutdown_event.set()


@pytest.fixture
def window():
    from littlebot.games.service import GamesService
    from littlebot.screens.service import ScreenService

    rt = AsyncRuntime("edge-controls-test")
    rt.start()
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.5})
    ctx.runtime = rt
    ctx.face = FaceController(ctx, seed=2)
    ctx.audio = FakeAudio(ctx.bus)
    ctx.games = GamesService(ctx, seed=1)
    ctx.screens = ScreenService(ctx)
    loop = D.DisplayLoop(_Robot(ctx, rt), max_frames=2)
    loop.open()
    loop.touch_bounce_s = 0.0  # lifts at once (tests/face/test_face_touch_bounce.py has the bounce)
    yield loop, ctx
    loop.close()
    ctx.screens.detach()
    ctx.games.close()
    ctx.face.stop_ticker()
    rt.stop()
    ctx.db.close()


def _wait(cond, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _post(loop, *events):
    for kind, pos in events:
        if kind == "down":
            pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
        elif kind == "move":
            pygame.event.post(pygame.event.Event(pygame.MOUSEMOTION, pos=pos, rel=(0, 0), buttons=(1, 0, 0)))
        else:
            pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1))
    loop._handle_events()


def _at(loop, nx, ny):
    r = loop.face_rect
    return (r.x + int(nx * r.w), r.y + int(ny * r.h))


def test_a_tap_on_the_edge_still_giggles(window):
    loop, ctx = window
    _post(loop, ("down", _at(loop, 0.96, 0.5)))
    assert ctx.face.status()["expression"] != "happy"  # held back: it may become a slide
    _post(loop, ("up", _at(loop, 0.96, 0.5)))
    assert ctx.face.status()["expression"] == "happy"
    assert _wait(lambda: ctx.audio.sounds == [D.TAP_REACTION_SOUND])
    assert ctx.audio.volume == 0.5  # unchanged


def test_a_tap_in_the_middle_giggles_at_once(window):
    loop, ctx = window
    _post(loop, ("down", _at(loop, 0.5, 0.5)))
    assert ctx.face.status()["expression"] == "happy"


def test_a_long_press_on_the_edge_still_opens_the_games_menu(window):
    loop, ctx = window
    _post(loop, ("down", _at(loop, 0.04, 0.5)))
    loop.check_edges(time.monotonic() + EC.STILL_SECONDS + 0.01)  # a resting finger: its giggle plays...
    assert ctx.face.status()["expression"] == "happy"
    assert loop.check_long_press(time.monotonic() + D.LONG_PRESS_SECONDS + 0.05)  # ...and the hold opens the menu
    assert _wait(lambda: ctx.screens.active) and ctx.screens.current.kind == "games"


def test_an_edge_swipe_sets_the_volume_without_a_giggle_or_the_menu(window, caplog):
    loop, ctx = window
    with caplog.at_level(logging.INFO, logger="littlebot.face.display"):
        _post(loop, ("down", _at(loop, 0.95, 0.8)), ("move", _at(loop, 0.95, 0.75)),
              ("move", _at(loop, 0.95, 0.8 - sweep(0.25))))
        assert loop.edges.active and loop._press is None and loop._edge_tap is None
        _post(loop, ("up", _at(loop, 0.95, 0.8 - sweep(0.25))))
    assert _wait(lambda: abs(ctx.audio.volume - 0.7) < 0.015)  # 0.5 + a quarter of 0..0.8
    assert ctx.face.status()["expression"] != "happy"
    assert loop.check_long_press(time.monotonic() + 5) is False and not ctx.screens.active
    assert _wait(lambda: "blip" in ctx.audio.sounds) and D.TAP_REACTION_SOUND not in ctx.audio.sounds
    assert "touch: volume 50% -> 70%" in caplog.text
    bar = loop.edges.bar()
    assert bar.name == "volume" and bar.alpha == 1.0


def test_a_brightness_swipe_dims_and_is_saved(window, caplog):
    loop, ctx = window
    with caplog.at_level(logging.INFO, logger="littlebot.face.display"):
        _post(loop, ("down", _at(loop, 0.04, 0.3)), ("move", _at(loop, 0.04, 0.35)),
              ("move", _at(loop, 0.04, 0.3 + sweep(0.4 / 0.85))))
        _post(loop, ("up", _at(loop, 0.04, 0.3 + sweep(0.4 / 0.85))))
    assert loop.brightness.level == pytest.approx(0.6, abs=0.015)
    assert "touch: brightness 100% -> 60% (software dimming)" in caplog.text
    assert _wait(lambda: ctx.settings.get("display.brightness") == loop.brightness.level)
    stored = Settings(ctx.db, EventBus(), build_defaults(ctx.config))  # what the next start reads
    assert stored.get("display.brightness") == loop.brightness.level
    assert ctx.audio.sounds == []  # no blip for the brightness


def test_software_dimming_is_applied_to_the_drawn_frame(window):
    loop, _ctx = window
    win = loop.window
    probe = (loop.face_rect.centerx, loop.face_rect.y + 6)  # plain background, no bar there
    loop._draw(time.monotonic())
    bright = win.get_at(probe)
    loop.brightness.set(0.4)
    loop._draw(time.monotonic())
    dim = win.get_at(probe)
    for a, b in zip(bright[:3], dim[:3]):
        assert b == pytest.approx(a * 0.4, abs=4)


def test_the_saved_brightness_is_restored_and_clamped(runtime_free_ctx):
    ctx = runtime_free_ctx
    ctx.settings.set("display.brightness", 0.5)
    loop = D.DisplayLoop(_Robot(ctx, None), max_frames=1)
    assert loop.brightness.level == 0.5
    ctx.settings.set("display.brightness", 0.0)  # the validator the display registers: never black
    assert ctx.settings.get("display.brightness") == EC.BRIGHTNESS_FLOOR


@pytest.fixture
def runtime_free_ctx():
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0})
    ctx.face = FaceController(ctx, seed=2)
    yield ctx
    ctx.face.stop_ticker()
    ctx.db.close()


def test_no_edge_swipes_over_a_card(window):
    loop, ctx = window
    from littlebot.screens.cards import games as G

    ctx.screens.show_card(G.GamesMenuCard({"card": "games"}), source="touch")
    _post(loop, ("down", _at(loop, 0.95, 0.8)), ("move", _at(loop, 0.95, 0.4)), ("up", _at(loop, 0.95, 0.4)))
    assert not loop.edges.active and loop.edges.bar() is None and ctx.audio.volume == 0.5


def test_no_edge_swipes_under_the_setup_banner(window):
    loop, ctx = window
    loop.setup_banner = {"active": True, "code": "123456"}
    _post(loop, ("down", _at(loop, 0.95, 0.8)), ("move", _at(loop, 0.95, 0.4)), ("up", _at(loop, 0.95, 0.4)))
    assert ctx.audio.volume == 0.5 and loop.edges.bar() is None


def test_keys_step_the_volume_and_brightness(window, caplog):
    loop, ctx = window
    with caplog.at_level(logging.INFO, logger="littlebot.face.display"):
        assert loop.handle_key(pygame.K_EQUALS, 0)
        assert _wait(lambda: ctx.audio.volume == pytest.approx(0.6))
        loop.handle_key(pygame.K_MINUS, 0)
        loop.handle_key(pygame.K_MINUS, 0)
        loop.handle_key(pygame.K_LEFTBRACKET, 0)
    assert loop.brightness.level == pytest.approx(0.9)
    assert "keys: volume 50% -> 60%" in caplog.text and "keys: brightness 100% -> 90%" in caplog.text
    loop.handle_key(pygame.K_RIGHTBRACKET, 0)
    assert loop.brightness.level == 1.0
    keys = [k for k, _ in D.HELP_LINES]
    assert "- / =" in keys and "[ / ]" in keys


def test_a_volume_change_elsewhere_shows_the_bar(window):
    loop, ctx = window
    ctx.settings.set("audio.volume", 0.3, source="dashboard")
    assert loop.edges.bar() is None  # noted on the bus thread, shown by the display loop
    loop.check_edges(time.monotonic())
    bar = loop.edges.bar()
    assert bar.name == "volume" and bar.value == 0.3
