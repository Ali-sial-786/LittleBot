"""Timers behind another card (littlebot/screens/service.py, "Timers behind another card"): a running timer is
not lost when another card, a video or a game takes the screen; it still rings and comes back."""

from __future__ import annotations

import json

import pytest
from screens_helpers import DummyCard, events

from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState
from littlebot.screens import service as S
from littlebot.screens.cards.timer import TimerCard


def voice(ctx):
    """A live voice call: it holds the face, speaker and microphone (not the screen)."""
    return ctx.arbiter.acquire("live_voice", [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT)


def run(svc, clock, seconds: float, step: float = 0.25) -> None:
    t = 0.0
    while t < seconds - 1e-9:
        clock.advance(step)
        svc.tick()
        t += step


async def timer_then_card(svc, **timer):
    assert (await svc.show({"card": "timer", "seconds": 300, **timer}))["ok"]
    card = svc.current
    assert isinstance(card, TimerCard)
    assert svc.show_card(DummyCard({"title": "Weather"}))["ok"]
    return card


async def test_a_timer_carries_on_behind_another_card_and_comes_back(svc, sctx, clock):
    call = voice(sctx)
    timer = await timer_then_card(svc, label="pasta")
    assert isinstance(svc.current, DummyCard) and svc.behind == [timer]
    assert svc.status()["last"]["reason"] == "replaced"
    assert svc.timers_now() == "Pasta timer: 5:00 left (behind the card on the screen)"
    run(svc, clock, 100)
    assert svc.timers_now() == "Pasta timer: 3:20 left (behind the card on the screen)"
    assert svc.status()["behind"] == [{"kind": "timer", "showing": "Pasta timer: 3:20 left", "hidden": False}]
    shown = events(sctx, S.CARD_SHOWN)
    call.release()  # the chat is over: the weather card lingers, goes, and the timer is back
    run(svc, clock, 30.25)
    assert svc.current is None and svc.behind == [timer]  # the screen is free: a second, in case a video takes it
    run(svc, clock, S.BACK_AFTER + 0.5)
    assert svc.current is timer and svc.behind == [] and timer.describe() == "Pasta timer: 2:49 left"
    assert [dict(e.data) for e in shown] == [{"kind": "timer", "title": "Pasta timer", "source": "timer"}]
    assert svc.timers_now() == "Pasta timer: 2:49 left (on the screen)"


async def test_a_timer_behind_rings_and_takes_the_screen_back(svc, sctx, clock):
    voice(sctx)
    news = events(sctx, S.SCREEN_NEWS)
    timer = await timer_then_card(svc, label="egg")
    run(svc, clock, 299)
    assert isinstance(svc.current, DummyCard) and not news
    run(svc, clock, 1.5)
    assert svc.current is timer and timer.describe() == "Egg timer: done!"
    assert [e.data["text"] for e in news] == ["The egg timer is done!",
                                              "The egg timer (5 minutes) has just finished; the screen shows Done!"]
    assert svc.status()["last"]["reason"] == "replaced"  # the weather card made way for it


async def test_a_timer_put_away_comes_back_only_when_it_rings(svc, sctx, clock):
    assert (await svc.show({"card": "timer", "seconds": 60}))["ok"]
    timer = svc.current
    svc.close("tap")  # a tap on the presenter's eyes
    assert not svc.active and svc.behind == [timer]
    assert svc.timers_now() == "Timer: 1:00 left (off the screen, still running)"
    run(svc, clock, 30)
    assert not svc.active  # the screen is free, but it was put away
    run(svc, clock, 31)
    assert svc.current is timer and timer.ringing
    run(svc, clock, 31)
    assert not svc.active and svc.behind == [] and svc.status()["last"]["reason"] == "done"


async def test_a_new_timer_joins_the_card_behind_and_commands_reach_it(svc, sctx, clock):
    voice(sctx)
    timer = await timer_then_card(svc, label="pasta")
    assert (await svc.show({"card": "timer", "seconds": 120, "label": "tea"}))["ok"]
    assert svc.current is timer and [t.label for t in timer.timers] == ["pasta", "tea"] and svc.behind == []
    svc.show_card(DummyCard({"title": "Recipe"}))
    assert svc.behind == [timer]
    assert svc.command("cancel", {"label": "pasta"}, card=timer)
    assert svc.timers_now() == "Tea timer: 2:00 left (behind the card on the screen)"
    assert not svc.command("picture", {"view": "fill"}, card=timer)
    assert svc.command("cancel", {}, card=timer) and svc.behind == [] and svc.timers_now() == ""
    assert isinstance(svc.current, DummyCard)


async def test_a_timer_that_already_rang_is_let_go(svc, sctx, clock):
    voice(sctx)
    assert (await svc.show({"card": "timer", "seconds": 10}))["ok"]
    run(svc, clock, 11)
    assert svc.current.ringing
    svc.show_card(DummyCard({"title": "Weather"}))  # they moved on: it must not come straight back
    assert svc.behind == [] and isinstance(svc.current, DummyCard)


async def test_a_video_covers_the_timer_which_still_rings(svc, sctx, clock):
    news = events(sctx, S.SCREEN_NEWS)
    assert (await svc.show({"card": "timer", "seconds": 60, "label": "tea"}))["ok"]
    timer = svc.current
    media = sctx.arbiter.acquire("media", [Resource.SCREEN, Resource.FACE, Resource.AUDIO_OUT], Priority.MEDIA)
    assert media is not None and not svc.active and svc.behind == [timer]
    run(svc, clock, 61)
    assert not svc.active and timer.ringing and news  # it rang (and told the voice) during the video
    media.release()
    run(svc, clock, 0.5)
    assert svc.current is timer


@pytest.mark.parametrize("how", ["stop all", "sleep", "switch"])
async def test_stop_all_sleep_and_the_switch_end_the_timers_behind(svc, sctx, how):
    voice(sctx)
    await timer_then_card(svc)
    if how == "stop all":
        sctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    elif how == "sleep":
        sctx.state.transition(RobotState.SLEEPING, "test")
    else:
        sctx.settings.set("screens.enabled", False)
    assert svc.behind == [] and not svc.active


async def test_keeps_littlebot_awake_while_a_timer_runs_behind(svc, sctx, clock):
    awake = events(sctx, S.KEEP_AWAKE)
    voice(sctx)
    await timer_then_card(svc)
    run(svc, clock, 130, step=1.0)
    assert len(awake) == 2


async def test_the_text_model_sees_and_steers_a_timer_behind_the_card(svc, sctx, clock):
    from littlebot.chatgpt.tools import RobotToolbox

    voice(sctx)
    timer = await timer_then_card(svc, label="egg")
    spec = next(s for s in RobotToolbox(sctx, mode="live").specs() if s["name"] == "change_card")
    assert spec["description"].endswith("running behind it: timer (Egg timer: 5:00 left), actions: add, new, pause, "
                                         "resume, cancel.")
    out = await RobotToolbox(sctx, mode="live").run("change_card", json.dumps({"action": "add",
                                                                               "args": {"seconds": 60}}))
    assert out.startswith("ok: Egg timer: 6:00 left (it runs off the screen") and isinstance(svc.current, DummyCard)
    out = await RobotToolbox(sctx, mode="live").run("change_card", json.dumps({"action": "cancel"}))
    assert out.startswith("ok: no timer any more") and timer.done and svc.behind == []
    assert "change_card" in {s["name"] for s in RobotToolbox(sctx, mode="live").specs()}  # the weather card


async def test_closing_a_timer_card_from_the_chat_keeps_its_timer(svc, sctx, clock):
    from littlebot.chatgpt.tools import RobotToolbox

    assert (await svc.show({"card": "timer", "seconds": 90}))["ok"]
    out = await RobotToolbox(sctx, mode="live").run("change_card", json.dumps({"action": "close"}))
    assert out.startswith("ok: the card is off the screen, but its timer still runs") and not svc.active
    spec = next(s for s in RobotToolbox(sctx, mode="live").specs() if s["name"] == "change_card")
    assert spec["description"].endswith("none on the screen; running off it: timer (Timer: 1:30 left), actions: "
                                         "add, new, pause, resume, cancel.")
