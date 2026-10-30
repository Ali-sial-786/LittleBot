"""The Reminders skill (littlebot/skills/reminders.py): setting, ringing, snoozing, repeats, restarts, cancelling."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from littlebot.skills import reminders as R
from littlebot.skills.base import ANNOUNCE
from littlebot.skills.hub import SkillHub
from littlebot.skills.reminders import Reminders
from littlebot.testing import make_test_context

TZ = ZoneInfo("America/Toronto")


def ts(*args: int) -> float:
    return dt.datetime(*args, tzinfo=TZ).timestamp()


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeScreens:
    def __init__(self) -> None:
        self.shown: list[dict[str, Any]] = []
        self.current = None

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append(dict(spec))
        return {"ok": True, "showing": "", "error": None}


class Rig:
    """A Reminders skill on a test robot (Toronto time, Friday 30 October 2026, 2:05 pm), recording what it did."""

    def __init__(self, **skills: Any) -> None:
        self.ctx = make_test_context(info={"timezone": "America/Toronto"}, skills=skills)
        self.clock = Clock(ts(2026, 10, 30, 14, 5))
        self.ctx.screens = FakeScreens()
        self.r = Reminders(self.ctx, now=self.clock)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.ctx.bus.subscribe("reminders.*", lambda e: self.events.append((e.topic, dict(e.data))))
        self.said: list[dict[str, Any]] = []
        self.ctx.bus.subscribe(ANNOUNCE, lambda e: self.said.append(dict(e.data)))

    @property
    def sounds(self) -> list[str]:
        return self.ctx.audio.sounds

    @property
    def shown(self) -> list[dict[str, Any]]:
        return self.ctx.screens.shown

    async def run(self, name: str, **args: Any) -> str:
        out = await self.r.run(name, args)
        await self.r.drain()
        return out

    async def at(self, *when: int) -> None:
        """Move the clock to ``when`` (Toronto) and tick."""
        self.clock.t = ts(*when)
        self.r.tick()
        await self.r.drain()

    async def wait(self, seconds: float, step: float = 0.5) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end - 1e-9:
            self.clock.t += step
            self.r.tick()
        await self.r.drain()

    def topics(self) -> list[str]:
        return [t for t, _ in self.events]


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.ctx.db.close()


async def test_set_list_and_context(rig: Rig) -> None:
    out = await rig.run("set_reminder", text="Remind me to call mom.", when="in 20 minutes")
    assert out == 'ok: reminder 1 "call mom" at 2:25 pm today (Friday 30 October), in 20 minutes'
    out = await rig.run("set_alarm", time="7am", repeat="weekdays", label="Gym alarm")
    assert out == 'ok: alarm 2 "Gym" at 7 am on Monday (Monday 2 November), every weekday, in 3 days'
    out = await rig.run("set_alarm", time="2026-10-31T06:30")
    assert out == "ok: alarm 3 at 6:30 am tomorrow (Saturday 31 October), in 16 hours 25 minutes"
    assert rig.shown[-1] == {"card": "reminders", "linger": R.SHOW_LINGER_S, "highlight": 3}
    assert rig.r.context_note() == ('Coming up: 2:25 pm today: call mom (id 1); 6:30 am tomorrow: alarm (id 3); '
                                    '7 am on Monday: alarm "Gym" (id 2, every weekday).')
    out = await rig.run("list_reminders")
    assert out.startswith("ok: 3 coming up: reminder 1 \"call mom\" at 2:25 pm today")
    assert rig.shown[-1] == {"card": "reminders"}
    assert (await rig.run("list_reminders", kind="alarms")).startswith("ok: 2 coming up: alarm 3")
    assert "already set" in await rig.run("set_reminder", text="call mom", when="at 2:25pm")
    assert len(rig.r.upcoming()) == 3


async def test_a_reminder_rings_once_with_a_chime_an_announcement_and_the_card(rig: Rig) -> None:
    await rig.run("set_reminder", text="call mom", when="at 6pm")
    await rig.at(2026, 10, 30, 17, 59, 59)
    assert rig.topics() == [] and rig.sounds == []
    await rig.at(2026, 10, 30, 18, 0, 0)
    assert rig.events == [("reminders.ringing", {"id": 1, "kind": "reminder", "label": "call mom"})]
    assert rig.sounds == ["chime"]
    assert rig.said == [{"text": "Reminder: call mom.", "kind": "reminder", "urgent": True,
                         "card": {"card": "reminders", "ringing": 1}}]
    assert rig.shown[-1] == {"card": "reminders", "ringing": 1}
    assert rig.r.context_note().startswith("A reminder is ringing now (call mom): stop_alarm or snooze stops it.")
    await rig.wait(R.REMINDER_RING_S)
    assert rig.sounds == ["chime"]  # a reminder chimes once
    assert rig.events[-1] == ("reminders.stopped", {"id": 1, "kind": "reminder", "label": "call mom",
                                                    "reason": "timeout"})
    assert rig.r.upcoming() == [] and rig.r.ringing() == [] and rig.r.item(1) is None
    assert rig.r.context_note() == ""


async def test_an_alarm_rings_until_stopped() -> None:
    rig = Rig()
    await rig.run("set_alarm", time="7am", label="wake up")
    await rig.at(2026, 10, 31, 7, 0, 0)
    assert rig.sounds == ["bell"] and rig.said[0]["text"] == "It's 7 am: wake up!" and rig.said[0]["kind"] == "alarm"
    await rig.wait(R.FIRST_BELL_GAP_S - 1)
    assert rig.sounds == ["bell"]  # quiet while the announcement is said
    await rig.wait(1 + 3 * R.BELL_EVERY_S)
    assert rig.sounds.count("bell") == 1 + 4
    assert await rig.run("stop_alarm") == "ok: stopped the wake up alarm"
    rings = rig.sounds.count("bell")
    await rig.wait(30)
    assert rig.sounds.count("bell") == rings
    assert rig.topics() == ["reminders.ringing", "reminders.stopped"]
    assert rig.events[-1][1]["reason"] == "stopped"
    assert rig.r.upcoming() == [] and await rig.run("stop_alarm") == "ok: nothing is ringing"


async def test_an_unlabelled_morning_alarm_says_good_morning_and_stops_by_itself() -> None:
    rig = Rig(alarm_ring_seconds=20)
    await rig.run("set_alarm", time="6:45am")
    await rig.at(2026, 10, 31, 6, 45)
    assert rig.said[0]["text"] == "Good morning! It's 6:45 am, time to get up."
    await rig.wait(19)
    assert rig.r.ringing()
    await rig.wait(1)
    assert not rig.r.ringing() and rig.events[-1][1]["reason"] == "timeout"


async def test_snooze_rings_again_later(rig: Rig) -> None:
    await rig.run("set_alarm", time="7am")
    await rig.at(2026, 10, 31, 7, 0)
    assert await rig.run("snooze") == "ok: snoozed the alarm for 9 minutes, until 7:09 am"
    assert rig.events[-1] == ("reminders.stopped", {"id": 1, "kind": "alarm", "label": "alarm", "reason": "snoozed",
                                                    "until": ts(2026, 10, 31, 7, 9)})
    assert "7:09 am today: alarm (id 1, snoozed)" in rig.r.context_note()
    await rig.at(2026, 10, 31, 7, 8, 59)
    assert not rig.r.ringing()
    await rig.at(2026, 10, 31, 7, 9)
    assert [i.id for i in rig.r.ringing()] == [1]
    assert await rig.run("snooze", minutes=5) == "ok: snoozed the alarm for 5 minutes, until 7:14 am"
    assert "error" in await rig.run("snooze", minutes=0) and "error" in await rig.run("snooze", minutes="soon")
    await rig.at(2026, 10, 31, 7, 14)
    assert rig.r.stop_ringing() and rig.r.upcoming() == []


async def test_snooze_just_after_a_reminder_went_quiet(rig: Rig) -> None:
    await rig.run("set_reminder", text="take the pizza out", when="in 10 minutes")
    await rig.wait(10 * 60 + R.REMINDER_RING_S + 1, step=1)
    assert not rig.r.ringing() and not rig.r.upcoming()
    out = await rig.run("snooze", minutes=3)
    assert out.startswith("ok: snoozed the reminder to take the pizza out for 3 minutes")
    assert [i.text for i in rig.r.upcoming()] == ["take the pizza out"]
    assert await rig.run("snooze") == "ok: nothing is ringing to snooze"  # once only


async def test_a_repeating_alarm_moves_on_and_skips_the_weekend(rig: Rig) -> None:
    await rig.run("set_alarm", time="7am", repeat="weekdays")
    item = rig.r.upcoming()[0]
    assert dt.datetime.fromtimestamp(item.due, TZ).strftime("%a %d %H:%M") == "Mon 02 07:00"
    await rig.at(2026, 11, 2, 7, 0)
    assert rig.r.ringing()
    nxt = rig.r.item(1)
    assert dt.datetime.fromtimestamp(nxt.due, TZ).strftime("%a %d %H:%M") == "Tue 03 07:00"
    rig.r.snooze(9)
    await rig.at(2026, 11, 2, 7, 9)
    assert rig.r.ringing()  # the snooze rang ...
    rig.r.stop_ringing()
    item = rig.r.upcoming()[0]  # ... and tomorrow's is still tomorrow's (not skipped)
    assert dt.datetime.fromtimestamp(item.fire_at, TZ).strftime("%a %d %H:%M") == "Tue 03 07:00"
    rig.said.clear()
    await rig.at(2026, 11, 6, 7, 1)  # Friday, a minute late (LittleBot was off Tuesday to now) ...
    assert rig.r.ringing() and [s["text"] for s in rig.said] == ["Good morning! It's 7:01 am, time to get up."]
    rig.r.stop_ringing()
    item = rig.r.upcoming()[0]
    assert dt.datetime.fromtimestamp(item.fire_at, TZ).strftime("%a %d %H:%M") == "Mon 09 07:00"


async def test_survives_a_restart_and_announces_what_it_missed(caplog: pytest.LogCaptureFixture) -> None:
    rig = Rig()
    await rig.run("set_reminder", text="call mom", when="at 3pm")
    await rig.run("set_reminder", text="water the plants", when="at 2:30pm")
    await rig.run("set_alarm", time="2:10pm", label="tea")
    await rig.run("set_reminder", text="dentist", when="tomorrow at 9")
    # LittleBot is off from 2:06 pm; it starts again at 4:20 pm.
    rig.clock.t = ts(2026, 10, 30, 16, 20)
    again = Reminders(rig.ctx, now=rig.clock)
    with caplog.at_level(logging.INFO, logger="littlebot.skills.reminders"):
        await again.start()
        await again.stop()
    await again.drain()
    assert [s["text"] for s in rig.said] == ["Sorry, I missed 2 reminders while I was off: water the plants at "
                                             "2:30 pm; call mom at 3 pm."]
    assert rig.said[0]["urgent"] is False
    assert any("dropped an alarm that was due 130 minutes ago" in r.getMessage() for r in caplog.records)
    assert [i.text for i in again.upcoming()] == ["dentist"]
    assert not again.ringing()


async def test_a_little_late_still_rings_and_what_rang_at_shutdown_is_let_go() -> None:
    rig = Rig()
    await rig.run("set_alarm", time="2:30pm")
    await rig.run("set_alarm", time="3pm", repeat="daily")
    await rig.at(2026, 10, 30, 14, 30)
    assert rig.r.ringing()  # LittleBot stops while it rings
    rig.clock.t = ts(2026, 10, 30, 15, 2)
    again = Reminders(rig.ctx, now=rig.clock)
    again.catch_up()
    await again.drain()
    assert [i.id for i in again.ringing()] == [2]  # two minutes late: it still rings
    assert again.item(1) is None  # the one that was ringing is gone
    again.stop_ringing()
    item = again.upcoming()[0]
    assert dt.datetime.fromtimestamp(item.fire_at, TZ).strftime("%a %d %H:%M") == "Sat 31 15:00"


async def test_cancel_by_id_words_time_and_all(rig: Rig) -> None:
    await rig.run("set_reminder", text="call mom", when="at 6pm")
    await rig.run("set_reminder", text="call mom", when="tomorrow at 6pm")
    await rig.run("set_alarm", time="7am", label="gym")
    await rig.run("set_alarm", time="6:30am", repeat="weekends")
    await rig.run("set_reminder", text="buy diapers", when="in 2 hours")
    out = await rig.run("cancel_reminder", text="call mom")
    assert out.startswith("ok: several match: reminder 1") and "reminder 2" in out and "Ask which" in out
    assert len(rig.r.upcoming()) == 5
    assert (await rig.run("cancel_reminder", id=2)).startswith('ok: cancelled reminder 2 "call mom"')
    assert (await rig.run("cancel_reminder", text="the 7am alarm")).startswith('ok: cancelled alarm 3 "gym"')
    assert (await rig.run("cancel_reminder", text="diaper")).startswith('ok: cancelled reminder 5 "buy diapers"')
    assert (await rig.run("cancel_reminder", text="the dentist")).startswith("error: nothing matches 'the dentist'")
    assert (await rig.run("cancel_reminder", id=99)).startswith("error:")
    assert (await rig.run("cancel_reminder", id="x")).startswith("error:")
    assert (await rig.run("cancel_reminder")).startswith("error:")
    assert (await rig.run("cancel_reminder", text="all alarms")).startswith("ok: cancelled alarm 4")
    assert [i.text for i in rig.r.upcoming()] == ["call mom"]


async def test_cancelling_one_that_rings_stops_it(rig: Rig) -> None:
    await rig.run("set_alarm", time="2:10pm")
    await rig.at(2026, 10, 30, 14, 10)
    assert rig.r.ringing()
    assert (await rig.run("cancel_reminder", id=1)).startswith("ok: cancelled alarm 1")
    assert not rig.r.ringing() and rig.events[-1][1]["reason"] == "cancelled"


@pytest.mark.parametrize("name, args, words", [
    ("set_reminder", {"when": "at 6pm"}, "what the reminder is for"),
    ("set_reminder", {"text": "x", "when": ""}, "give the when"),
    ("set_reminder", {"text": "x", "when": None}, "give the when"),
    ("set_reminder", {"text": "x", "when": ["at 6"]}, "give the when"),
    ("set_reminder", {"text": "x", "when": "whenever"}, "couldn't understand"),
    ("set_reminder", {"text": "x", "when": "at 6pm", "repeat": "hourly"}, "no hourly"),
    ("set_reminder", {"text": {"a": 1}, "when": "at 6pm"}, "what the reminder is for"),
    ("set_alarm", {}, "give the time"),
    ("set_alarm", {"time": "tomorrow"}, "needs a time of day"),
    ("set_alarm", {"time": "2026-10-29T07:00"}, "has passed"),
    ("nope", {}, "no tool"),
])
async def test_bad_arguments_are_errors_not_crashes(rig: Rig, name: str, args: dict[str, Any], words: str) -> None:
    out = await rig.run(name, **args)
    assert out.startswith("error:") and words in out
    assert rig.r.upcoming() == []


async def test_a_number_for_when_and_a_full_list(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await rig.run("set_alarm", time=7)).startswith("ok: alarm 1 at 7 pm today")
    monkeypatch.setattr(R, "MAX_PENDING", 2)
    assert (await rig.run("set_reminder", text="a", when="at 8pm")).startswith("ok:")
    assert "already 2" in await rig.run("set_reminder", text="b", when="at 9pm")


async def test_switched_off_and_the_hub() -> None:
    off = make_test_context(skills={"reminders": False})
    hub = SkillHub(off, [Reminders(off)])
    assert hub.specs() == [] and hub.context_notes() == ""
    assert (await Reminders(off).run("set_alarm", {"time": "7am"})).startswith("error: reminders are switched off")
    rig = Rig()
    hub = SkillHub(rig.ctx, [rig.r])
    names = [s["name"] for s in hub.specs()]
    assert names == ["set_reminder", "set_alarm", "list_reminders", "cancel_reminder", "snooze", "stop_alarm"]
    assert "set_alarm" in hub.prompt_notes()
    assert (await hub.run("set_reminder", '{"text": "stretch", "when": "in 1 hour"}')).startswith("ok: reminder 1")
    assert rig.r.status()["pending"] == 1


async def test_the_background_loop_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R, "TICK_S", 0.005)
    rig = Rig()
    await rig.run("set_reminder", text="stretch", when="in 1 minute")
    await rig.r.start()
    rig.clock.t += 61
    for _ in range(100):
        await asyncio.sleep(0.005)
        if rig.r.ringing():
            break
    await rig.r.stop()
    await rig.r.drain()
    assert rig.r.ringing() and rig.sounds == ["chime"]
    assert rig.r._task is None


async def test_the_home_time_zone_is_used() -> None:
    rig = Rig()
    rig.ctx.config.info.timezone = "Asia/Tokyo"
    rig.r._tz = False  # look it up again
    out = await rig.run("set_reminder", text="call grandma", when="at 9pm")
    assert "9 pm today (Saturday 31 October)" in out  # it is already Saturday 3:05 am in Tokyo
    rig.ctx.config.info.timezone = "Not/AZone"
    rig.r._tz = False
    rig.r.zone()  # an unknown zone falls back quietly (the system's zone)
    assert (await rig.run("set_reminder", text="stretch", when="in 5 minutes")).startswith("ok: reminder 2")
