"""ScreenService: showing a card, how it goes away, leases, the presenter's lip-sync, taps, keys, the host."""

from __future__ import annotations

import asyncio
import sys
from typing import ClassVar

import pytest
from screens_helpers import CARD_RGB, KIND, DummyCard, events

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.screens import service as S
from littlebot.screens.base import PRESENTER_BOX, H, W

pygame = pytest.importorskip("pygame")


def spec(**kw):
    return {"card": KIND, "title": "Hello", **kw}


def voice(ctx):
    """A live voice call: it holds the face, speaker and microphone like littlebot/chatgpt/live.py."""
    return ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT)


class Games:
    active = True


class Activities:
    def status(self):
        return {"running": True}


# ---------------------------------------------------------------- showing
async def test_show_builds_and_shows_the_card(svc, sctx):
    shown = events(sctx, S.CARD_SHOWN)
    r = await svc.show(spec(), source="voice")
    assert r == {"ok": True, "showing": "Hello (page 1)", "error": None}
    assert svc.active and isinstance(svc.current, DummyCard)
    assert svc.current.host.svc is svc  # the real host, not a recording one
    assert sctx.arbiter.holder(Resource.SCREEN).owner == S.OWNER
    assert [dict(e.data) for e in shown] == [{"kind": KIND, "title": "Hello", "source": "voice"}]
    st = svc.status()
    assert st["active"] and st["card"]["kind"] == KIND and st["source"] == "voice" and st["showing"] == "Hello (page 1)"
    assert st["enabled"] is True and st["web_pictures"] is True and st["linger_seconds"] == 30.0


async def test_show_errors(svc, sctx):
    assert (await svc.show({"card": "no_such_card"}))["error"] == "no_card"
    assert (await svc.show({"card": KIND}))["error"] == "no_card"  # required title missing
    assert (await svc.show(spec(empty=True)))["error"] == "no_card"  # the builder had nothing to show
    sctx.settings.set("screens.enabled", False)
    r = await svc.show(spec())
    assert r == {"ok": False, "showing": None, "error": "disabled"}
    assert svc.status()["last_error"] == "disabled" and not svc.active


async def test_config_switch_turns_it_off(svc, sctx):
    sctx.config.screens.enabled = False
    assert (await svc.show(spec()))["error"] == "disabled"


@pytest.mark.parametrize("why", ["game", "video", "activity", "asleep"])
async def test_busy_while_something_else_has_the_screen(svc, sctx, why):
    if why == "game":
        sctx.games = Games()
    elif why == "video":
        sctx.arbiter.acquire("media", [Resource.SCREEN, Resource.FACE, Resource.AUDIO_OUT], Priority.MEDIA)
    elif why == "activity":
        sctx.activities = Activities()
    else:
        sctx.state.transition(RobotState.SLEEPING, "test")
    r = await svc.show(spec())
    assert r["ok"] is False and r["error"] == "busy" and not svc.active


async def test_a_new_card_replaces_the_old_one(svc, sctx):
    closed = events(sctx, S.CARD_CLOSED)
    await svc.show(spec(title="One"))
    first = svc.current
    await svc.show(spec(title="Two"))
    assert svc.current is not first and svc.current.title == "Two" and first.closed
    assert [dict(e.data) for e in closed] == [{"kind": KIND, "reason": "replaced"}]
    assert sctx.arbiter.holder(Resource.SCREEN).owner == S.OWNER  # still ours, for the new card


def test_show_card_for_a_built_card(svc, sctx):
    card = DummyCard({"title": "Built"})
    r = svc.show_card(card, source="director")
    assert r["ok"] and svc.current is card and card.host.svc is svc
    assert svc.status()["source"] == "director"


# ---------------------------------------------------------------- how it goes away
def test_lingers_after_the_chat_then_goes(svc, sctx, clock):
    call = voice(sctx)
    svc.show_card(DummyCard({"title": "Weather"}))
    clock.advance(120)
    svc.tick()
    assert svc.active and svc.status()["closes_in"] is None  # the chat is on: it stays
    call.release()
    svc.tick()
    assert svc.status()["closes_in"] == 30.0
    clock.advance(29)
    svc.tick()
    assert svc.active
    closed = events(sctx, S.CARD_CLOSED)
    clock.advance(1.5)
    svc.tick()
    assert not svc.active and closed[0].data["reason"] == "chat over"
    assert svc.status()["last"]["reason"] == "chat over"
    assert call.active is False  # (released by the call itself, not by us)


def test_a_new_chat_keeps_a_lingering_card(svc, sctx, clock):
    svc.show_card(DummyCard({"title": "Weather"}))  # shown from the dashboard: no chat, lingers at once
    clock.advance(20)
    svc.tick()
    call = voice(sctx)
    clock.advance(20)
    svc.tick()
    assert svc.active
    call.release()
    svc.tick()  # (the watchdog checks every quarter second)
    clock.advance(25)
    svc.tick()
    assert svc.active
    clock.advance(6)
    svc.tick()
    assert not svc.active


def test_linger_comes_from_config(svc, sctx, clock):
    sctx.config.screens.linger_seconds = 5
    svc.show_card(DummyCard({"title": "x"}))
    clock.advance(5.1)
    svc.tick()
    assert not svc.active


def test_sticky_card_stays_until_done_and_keeps_littlebot_awake(svc, sctx, clock):
    awake = events(sctx, S.KEEP_AWAKE)
    svc.show_card(DummyCard({"title": "Timer", "sticky": True}))
    for _ in range(5):
        clock.advance(61)
        svc.tick()
    assert svc.active and len(awake) == 5 and svc.status()["closes_in"] is None
    assert svc.command("finish", {})
    assert not svc.active and svc.status()["last"]["reason"] == "done"


def test_card_that_sets_done_or_asks_to_close_is_closed(svc, clock):
    svc.show_card(DummyCard({"title": "a"}))
    svc.current.done = True
    svc.tick()
    assert not svc.active
    svc.show_card(DummyCard({"title": "b"}))
    svc.current.host.close()
    assert svc.active  # never mid-draw: the next check takes it off
    svc.tick()
    assert not svc.active and svc.status()["last"]["reason"] == "closed by the card"


@pytest.mark.parametrize("how", ["stop all", "sleep", "game event", "game lease", "video", "activity", "switch"])
def test_cards_close_when_something_else_starts(svc, sctx, how):
    svc.show_card(DummyCard({"title": "x"}))
    bus = sctx.bus
    if how == "stop all":
        bus.publish(Topics.STOP_ALL, {"source": "parent"})
    elif how == "sleep":
        sctx.state.transition(RobotState.SLEEPING, "idle timeout")
    elif how == "game event":
        bus.publish("game.started", {"id": "bubbles"})
    elif how == "game lease":
        assert sctx.arbiter.acquire("games", [Resource.SCREEN], Priority.ACTIVITY) is not None
    elif how == "video":
        bus.publish(Topics.MEDIA_STARTED, {"item_id": "x"})
    elif how == "activity":
        bus.publish(Topics.ACTIVITY_STARTED, {"id": "peekaboo"})
    else:
        sctx.settings.set("screens.enabled", False)
    assert not svc.active
    holder = sctx.arbiter.holder(Resource.SCREEN)
    assert holder is None or holder.owner != S.OWNER


def test_a_card_never_stops_the_voice(svc, sctx):
    call = voice(sctx)
    svc.show_card(DummyCard({"title": "x"}))
    assert call.active and sctx.arbiter.holder(Resource.FACE) is call
    svc.close("voice")
    assert call.active and sctx.arbiter.holder(Resource.MICROPHONE) is call


def test_close(svc, sctx):
    assert svc.close("nothing") is False
    svc.show_card(DummyCard({"title": "x"}))
    card = svc.current
    assert svc.close("voice") is True and card.closed and svc.current is None and svc.describe() is None
    assert sctx.arbiter.holder(Resource.SCREEN) is None
    assert svc.close("again") is False


# ---------------------------------------------------------------- voice steering
def test_command_goes_to_the_card(svc):
    assert svc.command("next", {}) is False  # no card
    svc.show_card(DummyCard({"title": "Recipe"}))
    assert svc.command("next", {}) is True and svc.describe() == "Recipe (page 2)"
    assert svc.command("fly", {}) is False


# ---------------------------------------------------------------- drawing
def test_render_draws_card_and_presenter_scaled_into_the_face(svc, sctx):
    assert svc.render(pygame.Surface((W, H)), None, 0.03) is False  # no card
    svc.show_card(DummyCard({"title": "x"}))
    win = pygame.Surface((960, 640))
    win.fill((0, 0, 0))
    rect = pygame.Rect(0, 0, 960, 640)
    assert svc.render(win, rect, 0.5) is True
    assert svc.current.draws == 1
    k = 960 / W
    assert tuple(win.get_at((int(600 * k), int(400 * k))))[:3] == CARD_RGB
    x, y, w, h = PRESENTER_BOX
    inside = {tuple(win.get_at((int((x + dx) * k), int((y + dy) * k))))[:3]
              for dx in range(5, w - 5, 9) for dy in range(5, h - 5, 9)}
    assert CARD_RGB not in inside or len(inside) > 3  # the presenter is drawn over the card
    assert svc.status()["drawn"] is True


def test_render_into_a_native_sized_rect(svc):
    svc.show_card(DummyCard({"title": "x"}))
    surf = pygame.Surface((W, H))
    assert svc.render(surf, surf.get_rect(), 0.03)
    assert tuple(surf.get_at((W - 10, H - 10)))[:3] == CARD_RGB


def test_broken_card_never_takes_the_face_down(svc):
    svc.show_card(DummyCard({"title": "x", "boom": True}))
    surf = pygame.Surface((W, H))
    for _ in range(S.MAX_FRAME_ERRORS - 1):
        assert svc.render(surf, None, 0.03) is False
    assert svc.active
    svc.render(surf, None, 0.03)
    assert not svc.active and svc.status()["last"]["reason"] == "error"


def test_presenter_mouth_follows_the_voice(svc, sctx, clock):
    svc.show_card(DummyCard({"title": "x"}))
    surf = pygame.Surface((W, H))
    s = svc._session
    for _ in range(10):
        sctx.bus.publish(Topics.AUDIO_VISEME, {"level": 0.8, "shape": "ah"})
        clock.advance(0.03)
        svc.render(surf, None, 0.03)
    assert s.mouth > 0.5 and s.shape == "ah" and svc.animating
    clock.advance(1.0)  # the voice went quiet
    for _ in range(10):
        clock.advance(0.03)
        svc.render(surf, None, 0.03)
    assert s.mouth == 0.0 and s.shape == ""


def test_animating_while_popping_in_then_calm(svc, clock):
    assert svc.animating is False
    svc.show_card(DummyCard({"title": "x"}))
    assert svc.animating  # popping in
    surf = pygame.Surface((W, H))
    for _ in range(12):
        clock.advance(0.1)
        svc.render(surf, None, 0.1)
    assert svc.animating is False
    svc.current.animating = True  # a card may ask for the full frame rate
    assert svc.animating


def test_headless_watchdog_keeps_the_card_clock_running(svc, clock):
    svc.show_card(DummyCard({"title": "x"}))
    for _ in range(8):
        clock.advance(0.25)
        svc.tick()
    assert svc.current.t == pytest.approx(2.0, abs=0.01)


async def test_watch_task_runs_on_the_loop(svc, sctx, clock):
    await svc.show(spec())
    svc.current.done = True
    for _ in range(20):
        await asyncio.sleep(0.05)
        if not svc.active:
            break
    assert not svc.active


# ---------------------------------------------------------------- taps and keys
def test_tap_on_the_eyes_closes_other_taps_go_to_the_card(svc):
    assert svc.handle_tap(0.5, 0.5) is False  # no card: the face gets it
    svc.show_card(DummyCard({"title": "x"}))
    card = svc.current
    assert svc.handle_tap(0.5, 0.5) is True
    assert card.taps == [(W * 0.5, H * 0.5)] and svc.active
    x, y, w, h = PRESENTER_BOX
    assert svc.handle_tap((x + w / 2) / W, (y + h / 2) / H) is True
    assert not svc.active and svc.status()["last"]["reason"] == "tap"


def test_tap_follows_a_moved_presenter_box(svc):
    card = DummyCard({"title": "x"})
    card.presenter_box = (560, 380, 150, 86)
    svc.show_card(card)
    svc.handle_tap(40 / W, 40 / H)  # the default corner is now card
    assert svc.active and card.taps
    svc.handle_tap(630 / W, 420 / H)
    assert not svc.active


def test_keys(svc):
    assert svc.handle_key("escape") is False
    svc.show_card(DummyCard({"title": "x"}))
    assert svc.handle_key("right") is True and svc.handle_key("x") is False  # unhandled keys reach the face
    assert svc.current.keys == ["right", "x"]
    assert svc.handle_key("escape") is True and not svc.active
    assert svc.status()["last"]["reason"] == "keyboard"


# ---------------------------------------------------------------- the host
async def test_host_tell_publishes_screen_news(svc, sctx):
    news = events(sctx, S.SCREEN_NEWS)
    await svc.show(spec(title="Timer"))
    svc.current.host.tell("The timer rang.")
    assert [dict(e.data) for e in news] == [{"kind": KIND, "title": "Timer", "text": "The timer rang."}]
    old = svc.current.host
    svc.close("x")
    old.tell("too late")  # a closed card's host stays quiet
    assert len(news) == 1


async def test_host_say_speaks_or_tells_the_voice(svc, sctx):
    news = events(sctx, S.SCREEN_NEWS)
    await svc.show(spec())
    host = svc.current.host
    host.say("Time's up!")
    await asyncio.sleep(0.01)
    assert sctx.audio.spoken == ["Time's up!"] and news == []
    call = voice(sctx)
    host.say("Time's up again!")
    await asyncio.sleep(0.01)
    assert sctx.audio.spoken == ["Time's up!"] and news[0].data["text"] == "Time's up again!"
    call.release()


async def test_host_sounds_are_throttled(svc, sctx, clock):
    await svc.show(spec())
    host = svc.current.host
    host.sound("pop")
    host.sound("pop")  # a tap storm: dropped
    clock.advance(0.5)
    host.sound("chime")
    await asyncio.sleep(0.01)
    assert sctx.audio.sounds == ["pop", "chime"]
    assert host.eye_color == tuple(sctx.config.face.eye_color)


def test_host_pictures(svc, sctx, monkeypatch):
    host = svc.make_host()
    monkeypatch.setitem(sys.modules, "littlebot.screens.images", None)  # images.py not there: no picture
    pic = host.picture("a red panda")
    assert pic.ready and pic.surface is None

    class Images:
        calls: ClassVar[list] = []

        @staticmethod
        def fetch(ctx, *, query="", url=""):
            Images.calls.append((ctx, query, url))
            return "PICTURE"

    monkeypatch.setitem(sys.modules, "littlebot.screens.images", Images)
    assert host.picture("a red panda") == "PICTURE" and Images.calls == [(sctx, "a red panda", "")]
    sctx.settings.set("screens.web_pictures", False)  # the parent's switch
    assert host.picture("a red panda").surface is None and len(Images.calls) == 1


# ---------------------------------------------------------------- wiring
def test_factory_and_composition_root(sctx):
    from littlebot.app.robot import SUBSYSTEMS
    from littlebot.screens import create_service

    names = [n for n, _m in SUBSYSTEMS]
    assert ("screens", "littlebot.screens") in SUBSYSTEMS and names.index("games") < names.index("screens")
    s = create_service(sctx)
    assert isinstance(s, S.ScreenService)
    s.detach()


def test_settings_and_config_defaults(sctx):
    assert sctx.settings.get("screens.enabled") is True
    assert sctx.settings.get("screens.web_pictures") is True
    assert sctx.config.screens.enabled is True and sctx.config.screens.linger_seconds == 30


async def test_lifecycle(svc):
    await svc.start()
    assert svc.service_status.value == "running"
    svc.show_card(DummyCard({"title": "x"}))
    await svc.stop()
    assert not svc.active and svc.service_status.value == "stopped"
