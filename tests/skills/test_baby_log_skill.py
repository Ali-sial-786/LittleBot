"""The baby log (littlebot/skills/baby_log.py): past times said many ways, feeds / diapers / sleep added up, the
context line that answers "when did she last eat?", undo, and the hub offering it."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from littlebot.chatgpt.tools import TOOL_NAMES
from littlebot.skills.baby_log import BabyLog, BadTime, ago_words, past_time
from littlebot.skills.hub import SkillHub
from littlebot.testing import make_test_context

HOME = ZoneInfo("America/Toronto")
NOW = dt.datetime(2026, 11, 3, 15, 0, tzinfo=HOME)  # a Tuesday, 3 pm


class Clock:
    def __init__(self) -> None:
        self.t = NOW.timestamp()

    def __call__(self) -> float:
        return self.t

    def at(self, hour: int, minute: int = 0) -> None:
        self.t = NOW.replace(hour=hour, minute=minute).timestamp()


@pytest.fixture
def rig():
    ctx = make_test_context(info={"timezone": "America/Toronto"})
    clock = Clock()
    yield BabyLog(ctx, now=clock), clock
    ctx.db.close()


async def run(b: BabyLog, name: str, **args: Any) -> str:
    return await b.run(name, args)


def test_past_times() -> None:
    def t(text: Any) -> str:
        return past_time(text, NOW).strftime("%a %H:%M")

    assert t(None) == t("now") == t("just now") == "Tue 15:00"
    assert t("20 minutes ago") == "Tue 14:40" and t("an hour ago") == "Tue 14:00"
    assert t("1 hour and 15 minutes ago") == "Tue 13:45" and t("half an hour ago") == "Tue 14:30"
    assert t("twenty minutes ago") == "Tue 14:40" and t("about 2 hours ago") == "Tue 13:00"
    assert t("at 2:30 pm") == "Tue 14:30" and t("2:30") == "Tue 14:30"  # the latest 2:30 that has passed
    assert t("9 pm") == "Mon 21:00" and t("at 9") == "Tue 09:00" and t("11:45") == "Tue 11:45"
    assert t("10 pm") == "Mon 22:00" and t("4:30") == "Tue 04:30" and t("3:05") == "Tue 03:05"
    assert t("3:01") == "Tue 15:00"  # a minute or two ahead of the robot's clock: just now
    assert t("2026-11-03T14:10") == "Tue 14:10" and t("2026-11-03T19:10:00Z") == "Tue 14:10"
    for bad in ("in 10 minutes", "2026-11-03T16:00", "2026-10-30T09:00", "teatime", "25:00", 4):
        with pytest.raises(BadTime):
            past_time(bad, NOW)
    assert ago_words(30) == "just now" and ago_words(25 * 60) == "25 minutes ago"
    assert ago_words(80 * 60) == "1 hour 20 minutes ago" and ago_words(6 * 3600 + 120) == "6 hours ago"


async def test_feeds_and_diapers_are_logged_and_added_up(rig) -> None:
    b, clock = rig
    out = await run(b, "log_baby", kind="feed", amount=4, unit="oz", detail="bottle", at="2:10 pm")
    assert out == "ok: logged a feed (4 oz, bottle) at 2:10 pm; today: 1 feed (4 oz in all)"
    await run(b, "log_baby", kind="feed", amount=3.5, unit="oz", at="2026-11-03T09:30")
    await run(b, "log_baby", kind="feed", detail="breast left", amount=15, unit="min", at="6:00")
    for detail in ("wet", "dirty", "both"):
        await run(b, "log_baby", kind="diaper", detail=detail)
    out = await run(b, "baby_summary")
    assert out == "ok: today so far: 3 feeds (7.5 oz, 15 min in all); 3 diapers (2 wet, 2 dirty)"
    out = await run(b, "baby_summary", kind="feed")
    assert out.startswith("ok: today so far: 3 feed entries: 6 am a feed (15 min, breast left), 9:30 am a feed")
    assert await run(b, "baby_summary", day="yesterday") == "ok: yesterday: nothing logged"
    clock.at(15, 50)
    note = b.context_note()
    assert note.startswith("Baby log: last feed 2:10 pm, 1 hour 40 minutes ago (4 oz, bottle); last diaper 3 pm, "
                           "50 minutes ago (both)")


async def test_sleep_and_waking(rig) -> None:
    b, clock = rig
    await run(b, "log_baby", kind="sleep", detail="nap", at="1 pm")
    assert "asleep since 1 pm (2 hours)" in b.context_note()
    out = await run(b, "log_baby", kind="wake", at="2:30 pm")
    assert out == "ok: logged waking up at 2:30 pm (slept 1 hour 30 minutes)"
    assert b.context_note() == "Baby log: awake since 2:30 pm."
    clock.at(15, 0)
    await run(b, "log_baby", kind="sleep", at="2:50 pm")
    assert b.context_note() == "Baby log: asleep since 2:50 pm (10 minutes)."
    assert await run(b, "baby_summary") == "ok: today so far: slept 1 hour 40 minutes; asleep now, since 2:50 pm"


async def test_asleep_over_midnight_counts_in_the_new_day(rig) -> None:
    b, clock = rig
    clock.t = NOW.replace(day=2, hour=20).timestamp()
    await run(b, "log_baby", kind="sleep", at="8 pm")
    clock.at(6, 30)
    await run(b, "log_baby", kind="wake")
    out = await run(b, "baby_summary")
    assert out == "ok: today so far: slept 6 hours 30 minutes"
    assert "slept 4 hours" in await run(b, "baby_summary", day="yesterday")


async def test_medicine_notes_undo_and_errors(rig) -> None:
    b, _ = rig
    out = await run(b, "log_baby", kind="medicine", detail="Tylenol", amount=2.5, unit="ml", at="noon")
    assert out.startswith("error:")  # "noon" isn't one of the phrases: the model sends a time
    out = await run(b, "log_baby", kind="medicine", detail="Tylenol", amount=2.5, unit="ml", at="12:00")
    assert out == "ok: logged medicine (Tylenol, 2.5 ml) at 12 pm"
    assert "last medicine 12 pm, 3 hours ago (Tylenol, 2.5 ml)" in b.context_note()
    assert (await run(b, "log_baby", kind="note")).startswith("error: give the note's words")
    await run(b, "log_baby", kind="note", detail="first smile!")
    assert "medicine (Tylenol, 2.5 ml) at 12 pm" in await run(b, "baby_summary")
    assert await run(b, "undo_baby_log") == "ok: took off a note: first smile at 3 pm"
    assert await run(b, "undo_baby_log", kind="medicine") == "ok: took off medicine (Tylenol, 2.5 ml) at 12 pm"
    assert await run(b, "undo_baby_log") == "ok: nothing to take off"
    assert b.context_note() == ""
    assert (await run(b, "log_baby", kind="bath")).startswith("error: kind must be one of")
    assert (await run(b, "log_baby", kind="feed", at="in an hour")).startswith("error:")
    assert (await run(b, "log_baby", kind="feed", amount="lots", unit="oz")) == \
        "ok: logged a feed at 3 pm; today: 1 feed"  # an amount that isn't a number is left out


async def test_old_entries_go_and_it_can_be_switched_off(rig) -> None:
    b, clock = rig
    await run(b, "log_baby", kind="diaper", detail="wet")
    clock.t += 61 * 86400
    await run(b, "log_baby", kind="diaper", detail="dirty")
    assert len(b.entries(0)) == 1
    b.ctx.config.skills.baby_log = False
    assert not b.available() and b.context_note() == ""
    assert (await run(b, "baby_summary")).startswith("error:")


def test_the_hub_offers_it_with_unique_tools() -> None:
    ctx = make_test_context()
    try:
        hub = SkillHub(ctx)
        assert hub.get("baby_log") is not None and hub.get("messages") is not None
        names = [s["name"] for s in hub.specs()]
        mine = {"log_baby", "baby_summary", "undo_baby_log", "leave_message", "messages_for", "message_given",
                "cancel_message"}
        assert mine <= set(names) and len(names) == len(set(names)) and not mine & set(TOOL_NAMES)
        for spec in hub.specs():
            if spec["name"] in mine:
                json.dumps(spec)
        notes = hub.prompt_notes()
        assert "log_baby" in notes and "leave_message" in notes
    finally:
        ctx.db.close()
