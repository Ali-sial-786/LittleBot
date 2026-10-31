"""The reminders card (littlebot/screens/cards/reminders.py): coming up, and ringing with Snooze / Stop, on the real
screen service with the Reminders skill."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from littlebot.screens import base, preview
from littlebot.screens.cards import reminders as card_mod
from littlebot.screens.cards.reminders import ENDED_S, SNOOZE_BUTTON, STOP_BUTTON, RemindersCard, time_label
from littlebot.screens.service import BACK_AFTER
from littlebot.skills.hub import SkillHub
from littlebot.skills.lists import Lists
from littlebot.skills.reminders import Reminders
from littlebot.testing import make_test_context

TZ = ZoneInfo("America/Toronto")


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Rig:
    def __init__(self) -> None:
        from littlebot.screens.service import ScreenService  # noqa: PLC0415

        self.ctx = make_test_context(info={"timezone": "America/Toronto"})
        self.wall = Clock(dt.datetime(2026, 10, 30, 14, 5, tzinfo=TZ).timestamp())
        self.mono = Clock(1000.0)
        self.r = Reminders(self.ctx, now=self.wall)
        self.lists = Lists(self.ctx, now=self.wall)
        self.ctx.skills = SkillHub(self.ctx, [self.r, self.lists])
        self.svc = ScreenService(self.ctx, clock=self.mono)
        self.ctx.screens = self.svc

    async def at(self, *when: int) -> None:
        self.wall.t = dt.datetime(*when, tzinfo=TZ).timestamp()
        self.r.tick()
        await self.r.drain()

    def frames(self, seconds: float, step: float = 0.1) -> None:
        """The display loop: the card's clock runs, the service checks it."""
        t = 0.0
        while t < seconds - 1e-9:
            self.mono.t += step
            self.svc.tick()
            card = self.svc.current
            if card is not None:
                card.update(step)
            self.svc.tick()
            t += step

    def close(self) -> None:
        self.svc.detach()
        self.ctx.db.close()


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.close()


def test_registered_and_offered() -> None:
    ct = base.card_types(offered_only=True)["reminders"]
    assert ct.fields == {} and ct.required == ()
    assert {"stop", "snooze", "next", "prev"} <= set(ct.actions)


def test_time_labels() -> None:
    now = dt.datetime(2026, 10, 30, 14, 5, tzinfo=TZ)
    assert time_label(now.replace(hour=18, minute=0), now) == "6 pm"
    assert time_label(now + dt.timedelta(days=1), now) == "Tomorrow 2:05 pm"
    assert time_label(now + dt.timedelta(days=3), now) == "Mon 2:05 pm"
    assert time_label(now + dt.timedelta(days=13), now) == "12 Nov 2:05 pm"
    assert time_label(now, now, twelve=False) == "14:05"


async def test_coming_up(rig: Rig) -> None:
    card = await base.build({"card": "reminders"}, rig.ctx, base.RecordingHost())
    assert isinstance(card, RemindersCard) and card.describe() == "No reminders or alarms coming up"
    assert card.title == "Coming up" and not card.sticky
    assert preview.surface(card, seconds=0.2).get_size() == (base.W, base.H)
    await rig.r.run("set_reminder", {"text": "call mom", "when": "at 6pm"})
    await rig.r.run("set_alarm", {"time": "7am", "repeat": "weekdays", "label": "gym"})
    await rig.r.run("set_reminder", {"text": "dentist", "when": "tomorrow at 9"})
    card.update(0.03)
    assert card.describe() == "Coming up, 3: 6 pm: call mom; Tomorrow 9 am: dentist; Mon 7 am: gym (alarm)"
    assert rig.svc.current is not None and rig.svc.current.kind == "reminders"  # the skill showed it
    assert rig.svc.current.highlight == 3 and rig.svc.current.linger_seconds == 12.0
    preview.surface(card, seconds=0.2)
    for i in range(4):
        await rig.r.run("set_reminder", {"text": f"thing {i}", "when": f"in {i + 1} hours"})
    card.update(0.03)
    assert card.pages == 2 and card.describe().startswith("Coming up, 7 (page 1 of 2): 3:05 pm: thing 0")
    assert card.tap(400, 300) and card.page == 1
    assert card.tap(400, 300) and card.page == 0  # round again
    assert card.key("right") and card.page == 1 and card.command("prev", {}) and card.page == 0
    assert not card.command("stop", {}) and not card.command("snooze", {})  # nothing rings on this view


async def test_an_alarm_rings_on_the_screen_and_is_snoozed_with_a_tap(rig: Rig) -> None:
    await rig.r.run("set_alarm", {"time": "7am", "label": "wake up"})
    await rig.at(2026, 10, 31, 7, 0)
    card = rig.svc.current
    assert isinstance(card, RemindersCard) and card.ring_id == 1 and card.sticky
    assert rig.svc.status()["sticky"] is True
    assert card.describe() == "Alarm ringing: wake up (it's 7 am); Snooze and Stop buttons on the screen"
    preview.surface(card, seconds=0.3)
    again = await base.build({"card": "reminders", "ringing": 1}, rig.ctx, card.host)
    assert again is card  # the announcement's card is the one already up
    rig.frames(5)
    assert rig.svc.current is card  # sticky: it stays
    host = base.RecordingHost()
    card.host = host
    assert not card.tap(360, 200)  # not on a button
    assert card.tap(*SNOOZE_BUTTON.center)
    assert host.sounds == ["click"] and host.told == ["The alarm was snoozed on the screen (Snoozed until 7:09 am)."]
    assert card.ended == "Snoozed until 7:09 am" and not card.sticky and not rig.r.ringing()
    assert card.describe() == "The alarm (wake up): Snoozed until 7:09 am"
    preview.surface(card, seconds=0.1)
    card.host = rig.svc.make_host()
    rig.frames(ENDED_S + 0.5)
    assert card.done and rig.svc.current is None
    await rig.at(2026, 10, 31, 7, 9)
    assert rig.r.ringing() and isinstance(rig.svc.current, RemindersCard) and rig.svc.current is not card


async def test_stop_with_a_tap_or_by_voice(rig: Rig) -> None:
    await rig.r.run("set_reminder", {"text": "call mom", "when": "at 6pm"})
    await rig.r.run("set_alarm", {"time": "6:30pm"})
    await rig.at(2026, 10, 30, 18, 0)
    card = rig.svc.current
    assert card.ring_kind == "reminder" and card.title == "Reminder"
    assert card.describe().startswith("Reminder ringing: call mom")
    assert card.tap(*STOP_BUTTON.center) and card.ended == "Stopped"
    assert rig.r.upcoming()[0].kind == "alarm" and rig.r.item(1) is None
    assert not card.tap(*STOP_BUTTON.center)  # already stopped
    rig.frames(ENDED_S + 0.5)
    assert rig.svc.current is None
    await rig.at(2026, 10, 30, 18, 30)
    card = rig.svc.current
    assert card.ring_id == 2 and card.sticky
    assert await rig.r.run("stop_alarm", {}) == "ok: stopped the alarm"  # the voice
    rig.frames(0.2)
    assert card.ended == "Stopped" and not card.sticky
    rig.frames(ENDED_S)
    assert rig.svc.current is None


async def test_voice_commands_on_the_card_and_when_it_stops_by_itself() -> None:
    rig = Rig()
    try:
        rig.ctx.config.skills.alarm_ring_seconds = 10
        await rig.r.run("set_alarm", {"time": "2:10pm"})
        await rig.at(2026, 10, 30, 14, 10)
        card = rig.svc.current
        assert rig.svc.command("snooze", {"minutes": 3})
        assert card.ended == "Snoozed until 2:13 pm"
        await rig.at(2026, 10, 30, 14, 13)
        card = rig.svc.current
        assert card.ring_id == 1 and rig.svc.command("stop", {}) and card.ended == "Stopped"
        await rig.r.run("set_alarm", {"time": "3pm"})
        await rig.at(2026, 10, 30, 15, 0)
        card = rig.svc.current
        assert card.sticky
        rig.wall.t += 11
        rig.r.tick()
        rig.frames(0.2)
        assert card.ended == "Stopped" and not card.sticky  # rang long enough
    finally:
        rig.close()


async def test_a_ringing_spec_for_one_that_stopped_shows_whats_coming_up(rig: Rig) -> None:
    await rig.r.run("set_reminder", {"text": "call mom", "when": "at 6pm"})
    card = await base.build({"card": "reminders", "ringing": 1}, rig.ctx, base.RecordingHost())
    assert card.ring_id is None and card.describe() == "Coming up, 1: 6 pm: call mom"


async def test_lists_changes_show_the_lists_card_briefly(rig: Rig) -> None:
    await rig.lists.run("add_to_list", {"items": ["milk"]})
    card = rig.svc.current
    assert card is not None and card.kind == "lists" and card.linger_seconds == 12.0
    assert rig.svc.status()["closes_in"] == 12.0  # no chat on: it lingers 12 s, not the usual 30
    await rig.r.run("set_alarm", {"time": "2:06pm"})
    await rig.at(2026, 10, 30, 14, 6)
    ringing = rig.svc.current
    assert ringing.kind == "reminders" and ringing.sticky
    await rig.lists.run("add_to_list", {"items": ["eggs"]})
    await rig.r.run("set_alarm", {"time": "9pm"})
    assert rig.svc.current is ringing  # a short look never covers the ringing alarm
    assert (await rig.lists.run("read_list", {})).startswith("ok: Shopping list, 2 items")
    assert rig.svc.current.kind == "lists" and rig.svc.behind == [ringing]  # asked for: shown, the alarm behind
    rig.svc._tick_behind()
    # ... and it stays up: straight back, the alarm hid the card they asked for (the robot, 2026-10-31)
    assert rig.svc.current.kind == "lists"
    rig.frames(rig.svc.status()["closes_in"] + BACK_AFTER + 0.5)  # the list card goes: the alarm, still ringing,
    assert rig.svc.current is ringing  # is back
    assert "Alarm ringing" in rig.svc.timers_now()
    rig.ctx.bus.publish("system.stop_all", {"source": "test"})  # the big stop button (once the loop runs)
    assert rig.r.ringing()  # the loop isn't running in this test: start() subscribes
    await rig.r.start()
    rig.ctx.bus.publish("system.stop_all", {"source": "test"})
    await rig.r.stop()
    rig.frames(0.2)
    assert not rig.r.ringing() and rig.svc.current is None  # the card went with STOP ALL too


async def test_previews_without_the_skill() -> None:
    for spec in card_mod.SAMPLES:
        card = await base.build(spec, None, base.RecordingHost())
        assert isinstance(card, RemindersCard)
        assert preview.surface(card, seconds=0.4).get_size() == (base.W, base.H)
    ringing = await base.build(card_mod.SAMPLES[1], None, base.RecordingHost())
    assert ringing.sticky and ringing.describe().startswith("Alarm ringing: Wake up")
    assert ringing.command("snooze", {}) and ringing.ended == "Snoozed" and not ringing.sticky
    listed = await base.build(card_mod.SAMPLES[0], None, base.RecordingHost())
    assert listed.describe().startswith("Coming up, 4: 6 pm: Call mom; Tomorrow 7 am: Wake up (alarm)")
    bad: dict[str, Any] = {"card": "reminders", "items": ["x", {"text": None}], "highlight": "x", "linger": "soon"}
    card = await base.build(bad, None, base.RecordingHost())
    assert card.describe() == "Coming up, 1: : Reminder" and card.linger_seconds is None
