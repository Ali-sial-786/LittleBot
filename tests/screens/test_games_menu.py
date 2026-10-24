"""The games menu card: every game in a list; drag to scroll, tap to play; opened by a long press."""

from __future__ import annotations

import asyncio
import time

import pytest
from screens_helpers import FakeClock

from littlebot.core.runtime import AsyncRuntime
from littlebot.face import display as D
from littlebot.face.controller import FaceController
from littlebot.games.catalog import GAMES
from littlebot.games.service import GamesService
from littlebot.screens import base, preview
from littlebot.screens.base import H, RecordingHost, W
from littlebot.screens.cards import games as G
from littlebot.screens.service import ScreenService
from littlebot.testing import make_test_context

pygame = pytest.importorskip("pygame")

NAMES = [g.name for g in GAMES]


def menu(**kw):
    started: list[str] = []

    def start(game_id, done):
        started.append(game_id)
        if kw.get("fail"):
            done(False, "LittleBot's screen is busy right now.")

    card = G.GamesMenuCard({"card": "games"}, RecordingHost(), start=start)
    return card, started


def row_centre(card, i):
    r = card.row_rect(i)
    return r.centerx, r.centery


# ---------------------------------------------------------------- the card
async def test_built_from_a_spec_lists_every_game_in_order(ctx):
    card = await base.build({"card": "games"}, ctx, RecordingHost())
    assert isinstance(card, G.GamesMenuCard)
    assert [g.name for g in card.games] == NAMES
    assert card.describe() == "The games: " + ", ".join(NAMES) + "."
    assert "games" in base.card_types() and base.card_types()["games"].required == ()


async def test_ages_filter(ctx):
    big = await base.build({"card": "games", "ages": "big"}, ctx, RecordingHost())
    assert [g.id for g in big.games] == ["memory", "tictactoe", "reaction", "rps"]
    little = await base.build({"card": "games", "ages": "little"}, ctx, RecordingHost())
    assert [g.audience for g in little.games] == ["little"] * 4
    assert len((await base.build({"card": "games", "ages": "child"}, ctx, RecordingHost())).games) == 8  # age unknown
    ctx.settings.set("profile", {**ctx.settings.get("profile"), "age_months": 10})
    child = await base.build({"card": "games", "ages": "child"}, ctx, RecordingHost())
    assert [g.id for g in child.games] == ["bubbles", "peekaboo", "colours", "star"]
    assert len((await base.build({"card": "games", "ages": "all"}, ctx, RecordingHost())).games) == 8


def test_blurbs_are_short_and_every_game_has_an_icon():
    for g in GAMES:
        assert len(G.blurb(g)) <= 26, g.id
        assert g.id in G._ICONS
        assert G.game_icon(g.id, 78, G.COLOURS[g.id]).get_size() == (78, 78)


def test_drag_scrolls_flings_and_springs_back():
    card, _ = menu()
    card.drag(0, -120)
    assert card.offset == pytest.approx(120) and card.animating
    card.drag_end()
    for _ in range(90):
        card.update(1 / 30)
    assert 120 < card.offset <= card.max_offset and not card.animating  # flung on, stopped inside the list
    card.drag(0, 2000)  # pulled far past the top
    card.drag_end()
    card.velocity = 0
    for _ in range(60):
        card.update(1 / 30)
    assert card.offset == 0.0


def test_arrow_keys_move_the_highlight_and_scroll_to_it():
    card, started = menu()
    for _ in range(7):
        assert card.key("down")
    assert card.selected == 7 and card.row_rect(7).bottom <= H and card.offset > 0
    assert card.key("up") and card.selected == 6
    assert card.key("return") and started == ["reaction"]
    assert card.key("q") is False


def test_tap_a_game_to_play_it():
    card, started = menu()
    assert card.tap(*row_centre(card, 1)) and started == ["peekaboo"] and card.starting == "peekaboo"
    assert card.tap(*row_centre(card, 0)) and started == ["peekaboo"]  # one at a time
    assert card.tap(W / 2, 40) is False  # the header


def test_a_game_that_cannot_start_says_why():
    card, started = menu(fail=True)
    card.tap(*row_centre(card, 0))
    assert started == ["bubbles"] and card.starting is None and "busy" in card.notice
    assert card.host.sounds == ["pop", "uhoh"]
    for _ in range(int(G.NOTICE_SECONDS * 30) + 5):
        card.update(1 / 30)
    assert card.notice == ""


def test_voice_commands():
    card, started = menu()
    assert card.command("scroll", {"direction": "down"}) and card.offset > 0
    assert card.command("scroll", {"direction": "up"}) and card.offset <= 0
    assert card.command("play", {"name": "Tic-Tac-Toe"}) and started == ["tictactoe"]
    assert card.command("play", {"id": "nope"}) is False and card.command("dance", {}) is False


def test_draws_with_the_presenter(tmp_path):
    card, _ = menu()
    out = preview.render(card, tmp_path / "games.png", seconds=1.0)
    img = pygame.image.load(str(out))
    assert img.get_size() == (W, H)
    card.offset = card.max_offset
    card.starting = "rps"
    card.say_notice("Busy")
    preview.render(card, tmp_path / "games_end.png", seconds=0.5)


# ---------------------------------------------------------------- with the services
@pytest.fixture
def robot():
    c = make_test_context()
    clock = FakeClock()
    c.games = GamesService(c, seed=1)
    c.screens = ScreenService(c, clock=clock)
    yield c, clock
    c.screens.detach()
    c.games.close()
    c.db.close()


async def test_opened_by_touch_outside_a_chat_and_tap_starts_the_game(robot):
    ctx, _clock = robot
    r = await ctx.screens.show({"card": "games"}, source="touch")
    assert r["ok"] and r["showing"].startswith("The games: Bubble Pop")
    card = ctx.screens.current
    x, y = row_centre(card, 2)
    assert ctx.screens.handle_press(x / W, y / H) and not card.starting  # waits: a tap or a drag?
    assert ctx.screens.handle_release(x / W, y / H) and card.starting == "colours"
    for _ in range(50):
        await asyncio.sleep(0.01)
        if ctx.games.active:
            break
    assert ctx.games.active and ctx.games.status()["id"] == "colours" and ctx.games.status()["source"] == "screen"
    assert not ctx.screens.active  # the game took the screen
    assert ctx.screens.status()["last"]["reason"] in ("game started", "preempted by games")


async def test_drag_through_the_service_scrolls_without_starting_a_game(robot):
    ctx, _clock = robot
    await ctx.screens.show({"card": "games"}, source="touch")
    card = ctx.screens.current
    ctx.screens.handle_press(0.5, 0.8)
    ctx.screens.handle_drag(0.5, 0.79)  # within the slop: nothing yet
    assert card.offset == 0
    ctx.screens.handle_drag(0.5, 0.6)
    ctx.screens.handle_drag(0.5, 0.4)
    assert card.offset == pytest.approx(0.4 * H, abs=1)
    ctx.screens.handle_release(0.5, 0.4)
    assert card.starting is None and not card.dragging and not ctx.games.active


async def test_closes_after_a_minute_without_a_touch(robot):
    ctx, clock = robot
    await ctx.screens.show({"card": "games"}, source="touch")
    clock.advance(50)
    ctx.screens.tick()
    assert ctx.screens.status()["closes_in"] == pytest.approx(10, abs=0.1)
    ctx.screens.handle_tap(0.5, 0.08)  # a touch (the header) starts the minute again
    clock.advance(50)
    ctx.screens.tick()
    assert ctx.screens.active
    clock.advance(11)
    ctx.screens.tick()
    assert not ctx.screens.active and ctx.screens.status()["last"]["reason"] == "chat over"


async def test_busy_while_a_game_runs(robot):
    ctx, _clock = robot
    assert await ctx.games.start("bubbles")
    r = await ctx.screens.show({"card": "games"}, source="touch")
    assert r["error"] == "busy"


# ---------------------------------------------------------------- the long press on the face
class _Robot:
    def __init__(self, ctx, runtime):
        import threading

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
    rt = AsyncRuntime("games-menu-test")
    rt.start()
    ctx = make_test_context(fakes=False, display={"headless": False, "show_sim_panel": False, "scale": 1.0})
    ctx.runtime = rt
    ctx.face = FaceController(ctx, seed=2)
    ctx.games = GamesService(ctx, seed=1)
    ctx.screens = ScreenService(ctx)
    loop = D.DisplayLoop(_Robot(ctx, rt), max_frames=2)
    loop.open()
    loop.touch_bounce_s = 0.0  # lifts at once (the touch-bounce tests below set it back)
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


def test_long_press_on_the_face_opens_the_games_menu(window):
    loop, ctx = window
    r = loop.face_rect
    loop.handle_click(r.center, 1, 0)  # a plain press: the usual reaction, and the hold starts
    assert loop._press is not None
    assert loop.check_long_press(time.monotonic()) is False  # not held long enough
    assert loop.check_long_press(time.monotonic() + D.LONG_PRESS_SECONDS + 0.05) is True
    assert _wait(lambda: ctx.screens.active)
    assert ctx.screens.current.kind == "games" and ctx.screens.status()["source"] == "touch"


def test_a_tap_or_a_wandering_finger_is_not_a_long_press(window):
    loop, ctx = window
    pg = pygame
    r = loop.face_rect
    loop.handle_click(r.center, 1, 0)
    pg.event.post(pg.event.Event(pg.MOUSEBUTTONUP, pos=r.center, button=1))
    loop._handle_events()
    assert loop._press is None  # released: just a tap
    loop.handle_click(r.center, 1, 0)
    pg.event.post(pg.event.Event(pg.MOUSEMOTION, pos=(r.centerx + r.w // 4, r.centery), rel=(0, 0), buttons=(1, 0, 0)))
    loop._handle_events()
    assert loop._press is None  # moved away: a swipe, not a hold
    assert loop.check_long_press(time.monotonic() + 5) is False and not ctx.screens.active


def test_drag_events_scroll_the_menu_in_the_window(window):
    loop, ctx = window
    ctx.screens.show_card(G.GamesMenuCard({"card": "games"}), source="touch")
    card = ctx.screens.current
    pg = pygame
    r = loop.face_rect
    start = (r.centerx, r.y + int(r.h * 0.8))
    loop.handle_click(start, 1, 0)
    for k in range(1, 6):
        pg.event.post(pg.event.Event(pg.MOUSEMOTION, pos=(start[0], start[1] - k * 30), rel=(0, -30),
                                     buttons=(1, 0, 0)))
    pg.event.post(pg.event.Event(pg.MOUSEBUTTONUP, pos=(start[0], start[1] - 150), button=1))
    loop._handle_events()
    assert card.offset > 100 and card.starting is None


def test_a_flickering_touch_screen_still_scrolls_and_doesnt_start_a_game(window):
    """A resistive screen: down, a lift for a few ms, down again, then moves that carry no button state."""
    loop, ctx = window
    loop.touch_bounce_s = D.TOUCH_BOUNCE_S
    ctx.screens.show_card(G.GamesMenuCard({"card": "games"}), source="touch")
    card = ctx.screens.current
    pg = pygame
    r = loop.face_rect
    start = (r.centerx, r.y + int(r.h * 0.8))
    pg.event.post(pg.event.Event(pg.MOUSEBUTTONDOWN, pos=start, button=1))
    pg.event.post(pg.event.Event(pg.MOUSEBUTTONUP, pos=start, button=1))  # the flicker
    pg.event.post(pg.event.Event(pg.MOUSEBUTTONDOWN, pos=(start[0], start[1] - 2), button=1))
    for k in range(1, 6):
        pg.event.post(pg.event.Event(pg.MOUSEMOTION, pos=(start[0], start[1] - k * 30), rel=(0, -30), buttons=(0, 0, 0)))
    pg.event.post(pg.event.Event(pg.MOUSEBUTTONUP, pos=(start[0], start[1] - 150), button=1))
    loop._handle_events()
    time.sleep(D.TOUCH_BOUNCE_S + 0.05)
    loop._handle_events()  # the last lift, once nothing followed it
    assert card.offset > 100 and card.starting is None and not card.dragging


def test_a_tap_is_held_back_a_moment_then_plays(window):
    loop, ctx = window
    loop.touch_bounce_s = D.TOUCH_BOUNCE_S
    ctx.screens.show_card(G.GamesMenuCard({"card": "games"}), source="touch")
    card = ctx.screens.current
    x, y = row_centre(card, 2)
    r = loop.face_rect
    pos = (r.x + int(x / W * r.w), r.y + int(y / H * r.h))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
    pygame.event.post(pygame.event.Event(pygame.MOUSEBUTTONUP, pos=pos, button=1))
    before = card.selected
    loop._handle_events()
    assert card.selected == before  # the finger may come straight back down (a flicker)
    time.sleep(D.TOUCH_BOUNCE_S + 0.05)
    loop._handle_events()
    assert card.selected == 2  # tapped (this card has no game starter: it only says so)
