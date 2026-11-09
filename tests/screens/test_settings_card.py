"""LittleBot's settings panel (a 5 s hold on the screen): volume, brightness, dark mode, the games, about."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from screens_helpers import FakeClock

from littlebot.core.runtime import AsyncRuntime
from littlebot.core.state import RobotState
from littlebot.games import art
from littlebot.games.catalog import GAMES
from littlebot.games.service import GamesService
from littlebot.screens import base, preview
from littlebot.screens.base import BODY_PX, PRESENTER_BOX, SMALL_PX, H, RecordingHost, W
from littlebot.screens.cards import settings as S
from littlebot.screens.service import ScreenService

pygame = pytest.importorskip("pygame")


async def make(ctx, **spec):
    card = await base.build({"card": "settings", **spec}, ctx, RecordingHost())
    assert isinstance(card, S.SettingsCard)
    return card


def press(card, cid):
    """Tap the middle of control ``cid`` (scrolled into the panel first, as a finger would)."""
    c = card.control(cid)
    assert c is not None, cid
    card.show_control(c)
    c = card.control(cid)
    assert S.VIEW.contains(c.rect) or not c.scrolls
    assert card.tap(*c.rect.center), cid
    return c


def wait_for(cond, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakePower:
    def __init__(self, percent=64, charging=False, fitted=True):
        self.st = {"fitted": fitted, "connected": fitted, "percent": percent if fitted else None,
                   "charging": charging, "charged": False, "plugged_in": charging}

    def state(self):
        return dict(self.st)


class _Dash:
    urls = ["http://192.168.1.42:8765/", "http://littlebot.local:8765/", "http://127.0.0.1:8765/"]
    port = 8765


class _Dashboard:
    dash = _Dash()


class _Robot:
    def __init__(self):
        self.dashboard = _Dashboard()
        self.slept: list[str] = []

    def sleep(self, source="parent"):
        self.slept.append(source)
        return True


# ---------------------------------------------------------------- the card type
async def test_registered_as_an_internal_card_that_lingers_a_minute(ctx):
    assert "settings" in base.card_types()
    assert "settings" not in base.card_types(offered_only=True)  # never offered to the AI
    card = await make(ctx)
    assert card.tab == "settings" and card.linger_seconds == pytest.approx(60)
    assert card.status()["kind"] == "settings" and card.status()["tab"] == "settings"
    assert (await make(ctx, tab="about")).tab == "about"
    assert (await make(ctx, tab="nonsense")).tab == "settings"


def test_steps_of_ten_percent():
    assert S.step_level(0.5, 1, 0, 0.8) == 0.6
    assert S.step_level(0.75, 1, 0, 0.8) == 0.8
    assert S.step_level(0.8, 1, 0, 0.8) == 0.8
    assert S.step_level(0.05, -1, 0, 0.8) == 0.0
    assert S.step_level(1.0, -1, 0.15, 1.0) == 0.9
    assert S.step_level(0.2, -1, 0.15, 1.0) == 0.15
    assert S.step_level(0.15, 1, 0.15, 1.0) == 0.2


# ---------------------------------------------------------------- the settings tab
async def test_volume_goes_through_the_speaker_and_stops_at_the_safe_maximum(ctx):
    ctx.audio.volume = 0.5
    card = await make(ctx)
    assert card.volume == 0.5 and card.max_volume == pytest.approx(0.8)
    press(card, "vol+")
    assert ctx.audio.volume == pytest.approx(0.6) and card.volume == pytest.approx(0.6)
    for _ in range(4):
        press(card, "vol+")
    assert ctx.audio.volume == pytest.approx(0.8) and card.volume == pytest.approx(0.8)  # never louder
    press(card, "vol-")
    assert ctx.audio.volume == pytest.approx(0.7)
    assert "click" in card.host.sounds and "boop" in card.host.sounds  # a click, and a boop at the end
    bar = card.control("vol:bar").rect
    card.tap(bar.x + bar.w * 0.21, bar.centery)  # a tap on the bar: that level (on the tens)
    assert ctx.audio.volume == pytest.approx(0.2)
    card.tap(bar.right - 2, bar.centery)
    assert ctx.audio.volume == pytest.approx(0.8)  # the far end is still the safe maximum
    card.update(1.0)  # (the speaker's volume isn't in the settings here: the card keeps what it set)
    assert card.volume == pytest.approx(0.8)


async def test_volume_without_a_speaker_sets_the_setting(ctx):
    ctx.audio = None
    card = await make(ctx)
    press(card, "vol+")
    assert ctx.settings.get("audio.volume") == pytest.approx(card.volume)


async def test_brightness_steps_down_to_the_floor_and_back(ctx):
    card = await make(ctx)
    assert card.brightness == 1.0
    press(card, "bright+")  # already the brightest
    assert ctx.settings.get("display.brightness") == 1.0
    for _ in range(12):
        press(card, "bright-")
    assert ctx.settings.get("display.brightness") == pytest.approx(0.15) and card.brightness == pytest.approx(0.15)
    press(card, "bright+")
    assert ctx.settings.get("display.brightness") == pytest.approx(0.2)


async def test_the_switches(ctx):
    card = await make(ctx)
    press(card, "dark")
    assert ctx.settings.get("display.theme") == "dark" and card.dark
    press(card, "dark")
    assert ctx.settings.get("display.theme") == "light" and not card.dark
    press(card, "flip")
    assert ctx.settings.get("display.flipped") is True and card.flipped
    press(card, "flip")
    assert ctx.settings.get("display.flipped") is False
    # Info cards off asks for a second tap (off closes every card, this one too)
    press(card, "cards")
    assert ctx.settings.get("screens.enabled") is True and card.confirm == "cards"
    assert "Tap again" in card._row_words("cards")[1]
    press(card, "cards")
    assert ctx.settings.get("screens.enabled") is False and not card.cards_on and card.confirm is None
    press(card, "cards")  # on again: at once
    assert ctx.settings.get("screens.enabled") is True
    press(card, "cards")
    for _ in range(int(S.CONFIRM_SECONDS * 10) + 2):
        card.update(0.1)
    assert card.confirm is None and ctx.settings.get("screens.enabled") is True  # the second tap never came


async def test_go_to_sleep_like_the_voice_tool(ctx):
    card = await make(ctx)
    press(card, "sleep")
    assert ctx.state.state == RobotState.SLEEPING
    card.update(0.1)
    assert card.sleeping and not card.notice
    robot = _Robot()
    ctx.extras["robot"] = robot
    ctx.state.transition(RobotState.IDLE, "test", force=True)
    card = await make(ctx)
    press(card, "sleep")
    assert robot.slept == ["screen"]  # robot.sleep, like RobotToolbox's go_to_sleep


async def test_a_change_made_elsewhere_shows_up(ctx):
    card = await make(ctx)
    ctx.settings.set("display.theme", "dark")  # the voice: "dark mode"
    ctx.settings.set("display.brightness", 0.4)  # an edge swipe
    ctx.settings.set("audio.volume", 0.3)  # the dashboard
    card.update(S.POLL_SECONDS + 0.01)
    assert card.dark and card.brightness == pytest.approx(0.4) and card.volume == pytest.approx(0.3)


async def test_a_change_that_fails_goes_back(ctx, monkeypatch):
    card = await make(ctx)

    def refuse(key, value, **kw):
        raise ValueError("no")

    monkeypatch.setattr(ctx.settings, "set", refuse)
    press(card, "flip")
    assert card.flipped  # at once on the screen...
    card.update(S.POLL_SECONDS + 0.01)
    assert not card.flipped  # ...and back when the robot refused it


def test_changes_run_on_the_robots_loop_not_the_display_thread(ctx):
    rt = AsyncRuntime("settings-card-test")
    rt.start()
    ctx.runtime = rt
    try:
        threads: list[str] = []
        real = ctx.settings.set
        gate = threading.Event()

        def slow_set(key, value, **kw):
            threads.append(threading.current_thread().name)
            gate.wait(2)  # SQLite being slow
            return real(key, value, **kw)

        ctx.settings.set = slow_set
        card = asyncio.run(make(ctx))
        t0 = time.monotonic()
        press(card, "dark")
        press(card, "bright-")
        press(card, "bright-")
        assert time.monotonic() - t0 < 0.5  # the taps never waited for it
        assert card.dark and card.brightness == pytest.approx(0.8)
        card.update(S.POLL_SECONDS + 0.01)
        assert card.dark and card.brightness == pytest.approx(0.8)  # nothing flips back while it is on its way
        gate.set()
        assert wait_for(lambda: ctx.settings.get("display.theme") == "dark"
                        and ctx.settings.get("display.brightness") == pytest.approx(0.8))
        assert threads and threading.main_thread().name not in threads
        assert len([k for k in threads]) <= 3  # (only the newest brightness, at most one before it)
    finally:
        rt.stop()


# ---------------------------------------------------------------- the games tab
async def test_games_tab_shows_every_game_and_a_tap_starts_it(ctx):
    clock = FakeClock()
    ctx.games = GamesService(ctx, seed=1)
    ctx.screens = ScreenService(ctx, clock=clock)
    try:
        r = await ctx.screens.show({"card": "settings", "tab": "games"}, source="touch")
        assert r["ok"]
        card = ctx.screens.current
        assert [g.id for g in card.games] == [g.id for g in GAMES]
        assert "Bubble Pop" in card.describe() and "Rock Paper Scissors" in card.describe()
        assert all(card.control(f"game:{g.id}").rect.h >= 90 for g in GAMES)
        c = card.control("game:colours")
        x, y = c.rect.center
        ctx.screens.handle_press(x / W, y / H)
        ctx.screens.handle_release(x / W, y / H)
        assert card.starting == "colours" and "pop" in card.host.sounds if isinstance(card.host, RecordingHost) \
            else card.starting == "colours"
        for _ in range(100):
            await asyncio.sleep(0.01)
            if ctx.games.active:
                break
        assert ctx.games.active and ctx.games.status()["id"] == "colours"
        assert ctx.games.status()["source"] == "screen"
        assert not ctx.screens.active  # the game took the screen
    finally:
        ctx.screens.detach()
        ctx.games.close()


async def test_a_game_that_cannot_start_says_why(ctx):
    card = await make(ctx, tab="games")
    card.link.start_game = lambda gid, done: done(False, "LittleBot's screen is busy right now.")
    press(card, "game:memory")
    card.update(0.1)
    assert card.starting is None and "busy" in card.notice and "uhoh" in card.host.sounds
    card.link.start_game = None
    card.notice = ""
    press(card, "game:rps")
    assert "can't start here" in card.notice


async def test_games_scroll_by_drag_and_spring_back(ctx):
    card = await make(ctx, tab="games")
    assert card.max_offset > 0
    card.drag(0, -100)
    assert card.offset == pytest.approx(100) and card.animating
    card.drag_end()
    for _ in range(90):
        card.update(1 / 30)
    assert 0 < card.offset <= card.max_offset
    card.drag(0, 3000)
    card.drag_end()
    card.velocity = 0
    for _ in range(60):
        card.update(1 / 30)
    assert card.offset == 0.0
    assert card.offsets["settings"] == 0.0  # each tab keeps its own place


# ---------------------------------------------------------------- the about tab
async def test_about_shows_the_dashboard_and_the_battery(ctx):
    ctx.extras["robot"] = _Robot()
    ctx.power = FakePower(64, charging=True)
    card = await make(ctx, tab="about")
    assert card.urls == ["192.168.1.42:8765", "littlebot.local:8765"] and not card.local_only
    assert card.battery == {"percent": 64, "charging": True, "full": False, "plugged_in": True}
    d = card.describe()
    assert "192.168.1.42:8765" in d and "64%" in d and "charging" in d
    ctx.power.st["percent"] = 63
    card.update(S.POLL_SECONDS + 0.01)
    assert card.battery["percent"] == 63  # kept up to date while it shows


async def test_about_without_a_battery_or_a_running_dashboard(ctx):
    ctx.power = FakePower(fitted=False)
    card = await make(ctx, tab="about")
    assert card.battery is None and "battery" not in card.describe().lower()
    assert len(card.urls) == 1 and card.urls[0].endswith(f".local:{ctx.config.dashboard.port}")
    ctx.power = None
    assert (await make(ctx, tab="about")).battery is None


async def test_about_says_when_only_the_robot_can_open_the_dashboard(ctx):
    robot = _Robot()
    robot.dashboard.dash = type("D", (), {"urls": ["http://127.0.0.1:8765/"], "port": 8765})()
    ctx.extras["robot"] = robot
    card = await make(ctx, tab="about")
    assert card.urls == ["127.0.0.1:8765"] and card.local_only


# ---------------------------------------------------------------- the voice, keys, layout
async def test_describe_and_status_for_the_voice(ctx):
    ctx.audio.volume = 0.4
    ctx.settings.set("display.theme", "dark")
    card = await make(ctx)
    d = card.describe()
    assert d.startswith("LittleBot's settings: volume 40%") and "dark mode on" in d
    assert "brightness 100%" in d and "info cards during chats on" in d and "right way up" in d
    st = card.status()
    assert st["volume"] == 0.4 and st["dark"] is True and st["brightness"] == 1.0 and st["cards"] is True
    assert card.command("tab", {"name": "games"}) and card.tab == "games"
    assert card.command("tab", {"name": "nope"}) is False and card.command("dance", {}) is False


async def test_tabs_and_close(ctx):
    card = await make(ctx)
    press(card, "tab:games")
    assert card.tab == "games" and card.host.sounds[-1] == "pop"
    press(card, "tab:about")
    assert card.tab == "about"
    press(card, "close")
    assert card.host.closed


async def test_arrow_keys_move_a_highlight_and_enter_presses(ctx):
    ctx.audio.volume = 0.5
    card = await make(ctx)
    assert card.focus == "tab:settings"
    assert card.key("down") and card.focus == "tab:games" and card.keyboard
    assert card.key("up") and card.focus == "tab:settings"
    assert card.key("up") and card.focus == "close"
    assert card.key("down") and card.focus == "vol+"
    assert card.key("left") and card.focus == "vol-"
    assert card.key("return")
    assert ctx.audio.volume == pytest.approx(0.4)
    assert card.key("down") and card.focus == "bright-"
    for want in ("dark", "flip", "cards", "sleep"):
        assert card.key("down") and card.focus == want
    assert S.VIEW.contains(card.control("sleep").rect)  # scrolled to it
    assert card.key("left") and card.focus.startswith("tab:")
    card.focus = "flip"
    assert card.key("space") and ctx.settings.get("display.flipped") is True
    card.focus = "tab:games"
    assert card.key("enter") and card.tab == "games"
    assert card.key("right") and card.focus.startswith("game:")
    assert card.key("q") is False


async def test_every_target_is_big_on_the_screen_and_clear_of_the_eyes(ctx):
    card = await make(ctx)
    px, py, pw, ph = PRESENTER_BOX
    eyes = pygame.Rect(px, py, pw, ph).inflate(2 * S.EYES_MARGIN, 2 * S.EYES_MARGIN)
    canvas = pygame.Rect(0, 0, W, H)
    for tab in S.TABS:
        card.open_tab(tab)
        card.offset = 0
        for c in card.controls():
            card.show_control(c)
            r = card.control(c.id).rect
            assert r.h >= 90 and r.w >= 90, c.id
            assert canvas.contains(r), c.id
            assert not r.colliderect(eyes), c.id
            if c.scrolls:
                assert S.VIEW.contains(r), c.id
        card.offset = 0
        shown = [c.rect for c in card.controls() if not c.scrolls or S.VIEW.contains(c.rect)]
        for i, a in enumerate(shown):
            for b in shown[i + 1:]:
                assert not a.colliderect(b), (a, b)
    assert not S.PANEL.colliderect(eyes) and not S.CLOSE.colliderect(eyes)
    assert S.BODY_PX == BODY_PX and SMALL_PX >= 28


async def test_taps_outside_the_panel_never_reach_rows_scrolled_out_of_it(ctx):
    card = await make(ctx)
    card.offset = card.max_offset
    vol = card.control("vol+").rect
    assert vol.bottom < S.VIEW.top  # scrolled up out of the panel
    assert card.control_at(vol.centerx, vol.centery) is None


async def test_draws_in_both_themes(ctx, tmp_path):
    ctx.extras["robot"] = _Robot()
    ctx.power = FakePower(12)
    try:
        corners = {}
        for dark in (False, True):
            art.set_dark(dark)
            for tab in S.TABS:
                card = await make(ctx, tab=tab)
                card.keyboard = True
                if tab == "games":
                    card.focus, card.starting = "game:star", "star"
                    card.say_notice("Busy")
                out = preview.render(card, tmp_path / f"{tab}_{'dark' if dark else 'light'}.png", seconds=0.4)
                img = pygame.image.load(str(out))
                assert img.get_size() == (W, H)
                corners[(dark, tab)] = img.get_at((W - 4, H - 4))
            card = await make(ctx)
            card.offset = card.max_offset
            card.confirm = "cards"
            card.sleeping = True
            preview.render(card, tmp_path / f"scrolled_{dark}.png", seconds=0.2)
        light, dark_px = corners[(False, "settings")], corners[(True, "settings")]
        assert sum(dark_px[:3]) < sum(light[:3]) - 200  # the night background
    finally:
        art.set_dark(False)


# ---------------------------------------------------------------- with the screen service
async def test_opened_by_touch_closes_on_close_esc_the_eyes_and_after_a_minute(ctx):
    clock = FakeClock()
    ctx.screens = ScreenService(ctx, clock=clock)
    try:
        r = await ctx.screens.show({"card": "settings"}, source="touch")
        assert r["ok"] and r["showing"].startswith("LittleBot's settings:")
        card = ctx.screens.current
        x, y = S.CLOSE.center
        ctx.screens.handle_press(x / W, y / H)
        ctx.screens.handle_release(x / W, y / H)
        assert not ctx.screens.active
        await ctx.screens.show({"card": "settings"}, source="touch")
        assert ctx.screens.handle_key("escape") and not ctx.screens.active
        await ctx.screens.show({"card": "settings"}, source="touch")
        bx, by, bw, bh = ctx.screens.current.presenter_box
        ctx.screens.handle_press((bx + bw / 2) / W, (by + bh / 2) / H)  # the eyes
        assert not ctx.screens.active
        await ctx.screens.show({"card": "settings"}, source="touch")
        card = ctx.screens.current
        clock.advance(50)
        ctx.screens.tick()
        x, y = card.control("dark").rect.center
        ctx.screens.handle_press(x / W, y / H)  # a touch starts the minute again
        ctx.screens.handle_release(x / W, y / H)
        assert card.dark
        clock.advance(55)
        ctx.screens.tick()
        assert ctx.screens.active
        clock.advance(6)
        ctx.screens.tick()
        assert not ctx.screens.active
    finally:
        ctx.screens.detach()


async def test_drag_through_the_service_scrolls_the_rows_without_pressing_them(ctx):
    ctx.screens = ScreenService(ctx, clock=FakeClock())
    try:
        await ctx.screens.show({"card": "settings"}, source="touch")
        card = ctx.screens.current
        x, y = card.control("flip").rect.center
        ctx.screens.handle_press(x / W, y / H)
        ctx.screens.handle_drag(x / W, (y - 60) / H)
        ctx.screens.handle_drag(x / W, (y - 150) / H)
        ctx.screens.handle_release(x / W, (y - 150) / H)
        assert card.offset > 100 and not card.flipped and ctx.settings.get("display.flipped") is False
    finally:
        ctx.screens.detach()
