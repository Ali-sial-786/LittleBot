"""The briefing skill (littlebot/skills/briefing.py): morning / evening / news briefings from the weather, the
reminders and lists skills and the news feeds, the card it puts up, and get_headlines. No network."""

from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

import pytest
from news_fakes import BBC_WORLD, CBC_HAMILTON, CBC_TOP, FakeFeeds, online_context

from littlebot.info.weather import WeatherError
from littlebot.screens.cards.weather import SAMPLE_REPORT
from littlebot.skills.base import Skill
from littlebot.skills.briefing import Briefing, card_spec, first_item, greeting, weather_line, weather_summary
from littlebot.skills.hub import SkillHub
from littlebot.skills.news import NewsFeeds

# Friday 30 October 2026, 7:05 am in Hamilton (EDT, UTC-4)
MORNING = dt.datetime(2026, 10, 30, 11, 5, tzinfo=dt.timezone.utc).timestamp()
NIGHT = dt.datetime(2026, 10, 31, 2, 30, tzinfo=dt.timezone.utc).timestamp()  # 10:30 pm


class StubWeather:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.asked: list[tuple[Any, int]] = []

    async def report(self, place: str | None = None, *, days: int = 2) -> dict[str, Any]:
        self.asked.append((place, days))
        if self.error:
            raise WeatherError(self.error)
        return copy.deepcopy(SAMPLE_REPORT)


class FakeReminders(Skill):
    name = "reminders"

    def context_note(self) -> str:
        return "Reminders: 9 am dentist; 6 pm call mom"

    def next_reminder(self) -> dict[str, str]:
        return {"when": "9 am", "text": "dentist"}


class FakeLists(Skill):
    name = "lists"

    def context_note(self) -> str:
        return "Lists: shopping (3 items), to-do (2 items)"


class BrokenLists(Skill):
    name = "lists"

    def context_note(self) -> str:
        raise RuntimeError("boom")


class FakeScreens:
    def __init__(self) -> None:
        self.shown: list[tuple[dict[str, Any], str]] = []

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append((spec, source))
        return {"ok": True}


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup():
    ctx = online_context(info={"timezone": "America/Toronto"})
    web = FakeFeeds()
    clock = Clock(MORNING)
    ctx.extras["weather"] = StubWeather()
    ctx.screens = FakeScreens()
    b = Briefing(ctx, now=clock, news=NewsFeeds(ctx, transport=web.transport()))
    ctx.skills = SkillHub(ctx, [b, FakeReminders(ctx), FakeLists(ctx)])
    yield ctx, b, web, clock
    ctx.db.close()


async def test_morning_briefing_has_everything(setup):
    ctx, b, web, _ = setup
    out = json.loads(await ctx.skills.run("get_briefing", '{"kind": "morning"}'))
    assert out["kind"] == "morning" and out["greeting"] == "Good morning"
    assert out["time"] == "7:05 am" and out["date"] == "Friday 30 October 2026"
    assert out["weather"] == {"place": "Toronto", "units": "°C", "day": "today", "high": 13, "low": 5,
                              "conditions": "partly cloudy", "chance_of_rain_or_snow_percent": 10,
                              "now": {"temperature": 12, "feels_like": 9, "conditions": "partly cloudy"}}
    assert out["reminders"] == "Reminders: 9 am dentist; 6 pm call mom"
    assert out["lists"] == "Lists: shopping (3 items), to-do (2 items)"
    assert [h["source"] for h in out["headlines"]] == ["CBC", "CBC Hamilton", "BBC World"]
    assert "20-40 second" in out["say"]
    assert ctx.extras["weather"].asked == [(None, 2)]
    spec, source = ctx.screens.shown[-1]
    assert source == "briefing"
    assert spec["card"] == "briefing" and spec["title"] == "Good morning" and spec["date"] == "Friday 30 October"
    assert spec["weather"] == "13° / 5° · partly cloudy" and spec["temperature"] == 12
    assert len(spec["headlines"]) == 3 and spec["sources"] == ["CBC", "CBC Hamilton", "BBC World"]
    assert spec["reminder"] == "9 am dentist"


async def test_context_note_remembers_todays_morning_briefing(setup):
    ctx, b, _, clock = setup
    assert b.context_note() == ""
    await b.run("get_briefing", {"kind": "morning"})
    assert "already given (at 7:05 am)" in b.context_note()
    assert "already given" in ctx.skills.context_notes()
    clock.t += 24 * 3600
    assert b.context_note() == ""
    assert b.status()["given"]["morning"] == ("2026-10-30", "7:05 am")


async def test_evening_briefing_is_about_tomorrow(setup):
    ctx, b, _, clock = setup
    clock.t = NIGHT
    out = json.loads(await b.run("get_briefing", {}))  # no kind at 10:30 pm: evening
    assert out["kind"] == "evening" and out["greeting"] == "Good night"
    assert out["weather"]["day"] == "tomorrow" and out["weather"]["high"] == 11 and "now" not in out["weather"]
    assert "wind-down" in out["say"]
    spec = ctx.screens.shown[-1][0]
    assert spec["title"] == "Good night" and spec["weather"] == "Tomorrow: 11° / 4° · light rain · rain 85%"


async def test_news_briefing_is_only_headlines(setup):
    ctx, b, _, _ = setup
    out = json.loads(await b.run("get_briefing", {"kind": "news"}))
    assert set(out) == {"kind", "greeting", "time", "date", "headlines", "say"}
    assert len(out["headlines"]) == 5  # skills.news_items
    assert ctx.extras["weather"].asked == []
    spec = ctx.screens.shown[-1][0]
    assert spec["title"] == "Top stories" and len(spec["headlines"]) == 5 and "reminder" not in spec


async def test_a_briefing_survives_the_weather_and_the_news_failing():
    ctx = online_context(info={"timezone": "America/Toronto"})
    try:
        ctx.extras["weather"] = StubWeather(error="no_location")
        web = FakeFeeds(status={CBC_TOP: 500, CBC_HAMILTON: 500, BBC_WORLD: 500})
        b = Briefing(ctx, now=Clock(MORNING), news=NewsFeeds(ctx, transport=web.transport()))
        out = json.loads(await b.run("get_briefing", {"kind": "morning"}))
        assert "doesn't know where home is" in out["weather"]["unavailable"]
        assert "couldn't be reached" in out["headlines_unavailable"]
        assert "reminders" not in out and "lists" not in out  # no skills hub: nothing to say about them
        assert out["greeting"] == "Good morning"
    finally:
        ctx.db.close()


async def test_a_broken_lists_skill_is_left_out():
    ctx = online_context(info={"timezone": "America/Toronto"})
    try:
        ctx.extras["weather"] = StubWeather()
        b = Briefing(ctx, now=Clock(MORNING), news=NewsFeeds(ctx, transport=FakeFeeds().transport()))
        ctx.skills = SkillHub(ctx, [b, BrokenLists(ctx)])
        out = json.loads(await b.run("get_briefing", {"kind": "morning"}))
        assert "lists" not in out and out["headlines"]
    finally:
        ctx.db.close()


async def test_offline_briefing_still_answers():
    ctx = online_context(mode="offline", info={"timezone": "America/Toronto"})
    try:
        web = FakeFeeds()
        b = Briefing(ctx, now=Clock(MORNING), news=NewsFeeds(ctx, transport=web.transport()))
        out = json.loads(await b.run("get_briefing", {"kind": "morning"}))
        assert "offline" in out["weather"]["unavailable"] and "offline" in out["headlines_unavailable"]
        assert web.requests == []
    finally:
        ctx.db.close()


async def test_get_headlines(setup):
    ctx, b, _, _ = setup
    out = json.loads(await ctx.skills.run("get_headlines", {"topic": "comet", "count": 3}))
    assert out == {"headlines": [{"title": "Scientists spot a new comet", "source": "BBC World"}], "topic": "comet"}
    assert (await b.run("get_headlines", {"topic": "cricket"})).startswith("ok: no headlines about cricket")
    out = json.loads(await b.run("get_headlines", {"count": "lots"}))
    assert len(out["headlines"]) == 5
    out = json.loads(await b.run("get_headlines", {"count": 99}))
    assert len(out["headlines"]) == 7  # every headline there is, once (15 at most)
    ctx.privacy.set(cloud_enabled=False)
    assert (await b.run("get_headlines", {})).startswith("error: cloud communication is off")


async def test_tools_prompt_and_switch(setup):
    ctx, b, _, _ = setup
    assert [t["name"] for t in b.tools()] == ["get_briefing", "get_headlines"]
    assert b.tools()[0]["parameters"]["properties"]["kind"]["enum"] == ["morning", "evening", "news"]
    assert "good morning" in b.prompt_note() and "get_briefing" in b.prompt_note()
    assert (await b.run("nope", {})).startswith("error:")
    off = online_context(skills={"briefing": False})
    try:
        assert Briefing(off).available() is False
    finally:
        off.db.close()


def test_helpers():
    assert greeting("morning", 7) == "Good morning" and greeting("morning", 14) == "Good afternoon"
    assert greeting("morning", 19) == "Good evening" and greeting("evening", 19) == "Good evening"
    assert greeting("evening", 23) == "Good night" and greeting("evening", 2) == "Good night"
    assert first_item("Reminders: 6 pm call mom; 8 pm bins") == "6 pm call mom"
    assert first_item("6:30 pm call mom. Then bed") == "6:30 pm call mom"
    assert first_item("") == ""
    assert weather_line(None) == "" and weather_line({"unavailable": "x"}) == ""
    assert weather_line({"high": 3, "low": -2, "conditions": "snow", "chance_of_rain_or_snow_percent": 60}) == \
        "3° / -2° · snow · rain 60%"
    w = weather_summary({"place": "Hamilton, Ontario", "units": {"temperature": "°F"}, "forecast": []}, 1)
    assert w == {"place": "Hamilton", "units": "°F", "day": "tomorrow"}
    local = dt.datetime(2026, 10, 30, 7, 5)
    spec = card_spec("morning", {"greeting": "Good morning", "headlines": [{"title": "A", "source": "X"}, "bad"]},
                     local, "")
    assert spec == {"card": "briefing", "title": "Good morning", "date": "Friday 30 October", "headlines": ["A"],
                    "sources": ["X"]}
