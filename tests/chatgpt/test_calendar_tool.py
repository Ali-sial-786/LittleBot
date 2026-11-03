"""The get_calendar tool in LittleBot's toolbox: offered once a calendar is added, answers from the calendars."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "info"))
from calendar_fakes import FakeGoogle, add_family, calendar_service  # noqa: E402

from littlebot.chatgpt.tools import CALENDAR_NOTE, RobotToolbox  # noqa: E402


async def run(box: RobotToolbox, name: str, **args) -> str:
    return await box.run(name, json.dumps(args))


def names(box: RobotToolbox) -> set[str]:
    return {s.get("name") for s in box.specs()}


async def test_offered_only_once_a_calendar_is_added(ctx):
    svc = calendar_service(ctx, FakeGoogle())
    box = RobotToolbox(ctx, mode="live")
    assert "get_calendar" not in names(box) and CALENDAR_NOTE not in box.instructions()
    assert "calendars" not in await run(box, "describe_yourself")
    add_family(svc)
    assert "get_calendar" in names(box) and "get_calendar" in names(RobotToolbox(ctx, mode="chat"))
    assert CALENDAR_NOTE in box.instructions()
    assert "family's calendars" in await run(box, "describe_yourself")


async def test_whats_on(ctx):
    ctx.privacy.set(cloud_enabled=True)
    ctx.config.info.timezone = "America/Toronto"
    add_family(calendar_service(ctx, FakeGoogle()))
    box = RobotToolbox(ctx, mode="live")
    today = json.loads(await run(box, "get_calendar"))
    assert [e["title"] for e in today["events"]] == ["Standup (long)", "Planning call"]
    work = json.loads(await run(box, "get_calendar", person="Ali", calendar="work", day="thursday"))
    assert [e["title"] for e in work["events"]] == ["Dentist - Dr Lee"]
    week = json.loads(await run(box, "get_calendar", person="Sam", day="this week", days="lots"))  # days: the words'
    assert week["to"] == "Sunday 8 November" and [e["title"] for e in week["events"]] == ["Family dinner",
                                                                                           "Swimming lessons"]
    found = json.loads(await run(box, "get_calendar", search="dentist"))
    assert found["events"][0]["date"] == "Thursday 5 November"
    assert box.calls == ["get_calendar"] * 4 and not box.pending


async def test_problems_are_plain_errors(ctx):
    add_family(calendar_service(ctx, FakeGoogle()))
    box = RobotToolbox(ctx)
    out = await run(box, "get_calendar")
    assert out.startswith("error: cloud communication is off in Privacy") and "don't guess" in out
    ctx.privacy.set(cloud_enabled=True)
    out = await run(box, "get_calendar", person="my")
    assert out.startswith("error: say whose calendar, by name: the calendars are Ali (Work, Personal) and Sam "
                          "(Personal)") and out.endswith("Ask them, briefly.")
    out = await run(box, "get_calendar", day="the twelfth of never")
    assert out.startswith("error: LittleBot couldn't tell which day")
