"""Heads-ups (littlebot/skills/heads_up.py) and phone notifications (push.py): a calendar event soon, rain coming,
reminders on the phone, quiet hours, the voice settings, send_to_phone, and that the ntfy topic never shows."""

from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from littlebot.skills.base import ANNOUNCE
from littlebot.skills.heads_up import HeadsUp, in_quiet, parse_quiet, weather_words, wet_kind
from littlebot.skills.hub import SkillHub
from littlebot.skills.push import Push
from tests.skills.calendar_fakes import MONDAY_10, make

TOPIC = "littlebot-secret-topic-xyz"


class FakeNtfy:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(self.status, json={"id": "x"})


class FakeWeather:
    def __init__(self, report: dict[str, Any]) -> None:
        self.data = report
        self.asked = 0

    def blocked_reason(self) -> None:
        return None

    async def report(self, place: Any = None, *, days: int = 2, hourly: bool = False) -> dict[str, Any]:
        self.asked += 1
        return self.data


def rain_report(now: str = "partly cloudy", hours: list[tuple[str, str, int]] | None = None) -> dict[str, Any]:
    hours = hours if hours is not None else [("11:00", "cloudy", 20), ("12:00", "light rain", 70),
                                             ("13:00", "rain", 90)]
    return {"now": {"conditions": now}, "hour_by_hour": [{"time": t, "conditions": c,
                                                          "chance_of_rain_or_snow_percent": p} for t, c, p in hours]}


def build(*, phone: bool = True, ntfy: FakeNtfy | None = None, **kw: Any):
    ctx, cal, google, clock = make(**kw)
    if phone:
        ctx.config.ntfy_topic = SecretStr(TOPIC)
    ntfy = ntfy or FakeNtfy()
    heads = HeadsUp(ctx, now=clock, push=Push(ctx, transport=httpx.MockTransport(ntfy)))
    ctx.skills = SkillHub(ctx, [cal, heads])
    said: list[dict[str, Any]] = []
    ctx.bus.subscribe(ANNOUNCE, lambda e: said.append(dict(e.data)))
    return ctx, cal, heads, clock, ntfy, said


@pytest.fixture
def env():
    got = build()
    yield got
    got[0].db.close()


# ------------------------------------------------------------------ helpers
def test_quiet_hours():
    q = parse_quiet("21:30-07:00")
    assert q == (21 * 60 + 30, 7 * 60)
    night = MONDAY_10.replace(hour=23)
    assert in_quiet(night, q) and in_quiet(MONDAY_10.replace(hour=6, minute=59), q)
    assert not in_quiet(MONDAY_10, q) and not in_quiet(MONDAY_10, None)
    assert in_quiet(MONDAY_10.replace(hour=13), parse_quiet("13:00-15:00"))
    for bad in ("", "off", "25:00-07:00", "9pm-7am", "07:00-07:00"):
        assert parse_quiet(bad) is None


def test_wet_words():
    assert wet_kind("light rain showers") == "rain" and wet_kind("heavy snow") == "snow"
    assert wet_kind("thunderstorms with hail") == "thunderstorms" and wet_kind("freezing drizzle") == "freezing rain"
    assert wet_kind("freezing fog") is None and wet_kind("cloudy") is None and wet_kind(None) is None
    assert weather_words("rain", "3 pm", within_hour=False) == ("Heads up: rain is likely around 3 pm. Take an "
                                                                "umbrella if you go out.")
    assert "within the hour" in weather_words("snow", "3 pm", within_hour=True)


# ------------------------------------------------------------------ calendar
async def test_a_calendar_event_soon_is_said_once_and_sent_to_the_phone(env):
    ctx, cal, heads, clock, ntfy, said = env
    clock.set(MONDAY_10.replace(hour=14, minute=30))
    await cal.refresh()
    await heads.tick()
    await heads.drain()
    assert [s["text"] for s in said] == ["Heads up: Dentist at 3 pm, in 30 minutes, at Main St Clinic."]
    assert said[0]["kind"] == "heads_up" and said[0]["card"]["card"] == "calendar"
    assert ntfy.sent == [{"topic": TOPIC, "title": "Dentist in 30 minutes", "message": "3 pm, at Main St Clinic",
                          "priority": 3, "tags": ["calendar"]}]
    clock.t += 30
    await heads.tick()
    await heads.drain()
    assert len(said) == 1 and len(ntfy.sent) == 1  # once
    assert ctx.audio.sounds[-1] == "chime" if hasattr(ctx.audio, "sounds") else True


async def test_not_for_an_event_added_a_moment_ago(env):
    ctx, cal, heads, clock, ntfy, said = env
    clock.set(MONDAY_10.replace(hour=11))
    await cal.refresh()
    await cal.run("add_calendar_event", {"title": "Call the bank", "start": "2026-11-02T11:20"})
    await heads.tick()
    assert said == []


async def test_quiet_hours_and_no_speaker_still_tell_the_phone(env):
    ctx, cal, heads, clock, ntfy, said = env
    ctx.settings.set("heads_up.quiet_hours", "14:00-16:00")
    clock.set(MONDAY_10.replace(hour=14, minute=30))
    await cal.refresh()
    await heads.tick()
    await heads.drain()
    assert said == [] and len(ntfy.sent) == 1
    assert heads.sent[-1] == {"kind": "calendar", "at": clock(), "spoken": False, "phone": True}


async def test_minutes_setting_and_off(env):
    ctx, cal, heads, clock, ntfy, said = env
    ctx.settings.set("heads_up.calendar_minutes", 60)
    clock.set(MONDAY_10.replace(hour=14))
    await cal.refresh()
    await heads.tick()
    assert said and said[0]["text"].startswith("Heads up: Dentist at 3 pm, in 1 hour")
    ctx.settings.set("heads_up.enabled", False)
    clock.set(MONDAY_10.replace(hour=17, minute=30))
    await cal.refresh()
    heads._told.clear()
    await heads.tick()
    assert len(said) == 1


# ------------------------------------------------------------------ weather
async def test_rain_coming_is_said_once(env):
    ctx, cal, heads, clock, ntfy, said = env
    weather = FakeWeather(rain_report())
    ctx.extras["weather"] = weather
    await heads.tick()
    await heads.drain()
    assert [s["text"] for s in said] == ["Heads up: rain is likely around 12 pm. Take an umbrella if you go out."]
    assert ntfy.sent[-1]["title"] == "Rain around 12 pm" and ntfy.sent[-1]["tags"] == ["umbrella"]
    clock.t += 3600
    await heads.tick()
    assert len(said) == 1  # not again for a while
    weather.data = rain_report(hours=[("17:00", "cloudy", 10), ("18:00", "thunderstorms", 80)])
    clock.t += 6 * 3600  # 17:00, more than 6 hours after the first: a new spell may be told
    await heads.tick()
    assert said[-1]["text"] == "Heads up: thunderstorms are likely around 6 pm."


async def test_no_heads_up_when_it_is_already_wet_or_unlikely(env):
    ctx, cal, heads, clock, ntfy, said = env
    ctx.extras["weather"] = FakeWeather(rain_report(now="light rain"))
    await heads.tick()
    ctx.extras["weather"] = FakeWeather(rain_report(hours=[("11:00", "light rain", 40), ("12:00", "cloudy", 10)]))
    heads._weather_t = -1e9
    await heads.tick()
    ctx.extras["weather"] = FakeWeather(rain_report(hours=[("11:00", "cloudy", 0)] * 3 + [("14:00", "rain", 99)]))
    heads._weather_t = -1e9
    await heads.tick()  # only the next 3 hours count
    assert said == []
    ctx.settings.set("heads_up.weather", False)
    ctx.extras["weather"] = FakeWeather(rain_report())
    heads._weather_t = -1e9
    await heads.tick()
    assert said == []


async def test_weather_waits_for_a_video_to_end(env):
    ctx, cal, heads, clock, ntfy, said = env
    ctx.extras["weather"] = FakeWeather(rain_report(hours=[("11:00", "snow", 80)]))

    class Media:
        state = "playing"

        def status(self) -> dict[str, Any]:
            return {"state": self.state}

    ctx.media = Media()
    await heads.tick()
    await heads.drain()
    assert said == [] and ntfy.sent[-1]["title"] == "Snow soon"  # the phone at once
    ctx.media.state = "idle"
    clock.t += 60
    await heads.tick()
    await heads.drain()
    assert [s["text"] for s in said] == ["Heads up: snow is likely within the hour. Dress warmly if you go out."]
    assert len(ntfy.sent) == 1  # not twice


async def test_weather_only_in_the_day(env):
    ctx, cal, heads, clock, ntfy, said = env
    ctx.extras["weather"] = FakeWeather(rain_report())
    clock.set(MONDAY_10.replace(hour=21))
    await heads.tick()
    assert said == [] and ctx.extras["weather"].asked == 0


# ------------------------------------------------------------------ reminders and sending
async def test_a_reminder_that_rings_goes_to_the_phone(env):
    ctx, cal, heads, clock, ntfy, said = env
    await heads.start()
    try:
        ctx.bus.publish("reminders.ringing", {"id": 1, "kind": "reminder", "label": "call mom"})
        ctx.bus.publish("reminders.ringing", {"id": 2, "kind": "alarm", "label": "alarm"})
        await heads.drain()
    finally:
        await heads.stop()
    assert [(n["title"], n["priority"]) for n in ntfy.sent] == [("Reminder: call mom", 4)]


async def test_send_to_phone(env):
    ctx, cal, heads, clock, ntfy, said = env
    assert "send_to_phone" in [t["name"] for t in heads.tools()]
    assert await heads.run("send_to_phone", {"title": "Shopping list", "text": "milk, eggs"}) == "ok: sent to the phone"
    assert ntfy.sent[-1]["message"] == "milk, eggs" and ntfy.sent[-1]["topic"] == TOPIC
    assert (await heads.run("send_to_phone", {})).startswith("error")


async def test_no_phone_no_tool_and_failures_say_why(caplog):
    caplog.set_level(logging.DEBUG)
    ctx, cal, heads, clock, ntfy, said = build(phone=False)
    assert "send_to_phone" not in [t["name"] for t in heads.tools()]
    assert not heads.phone_on()
    ctx.db.close()
    ctx, cal, heads, clock, ntfy, said = build(ntfy=FakeNtfy(status=429))
    assert await heads.run("send_to_phone", {"text": "hi"}) == "error: it didn't go (rate_limited)"
    ours = " ".join(r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore")))
    assert TOPIC not in ours + json.dumps(heads.status())
    ctx.privacy.set(cloud_enabled=False)
    assert await heads.push.send("x", "y") is False and heads.push.last_error == "cloud_off"
    ctx.db.close()


# ------------------------------------------------------------------ settings by voice
async def test_settings_by_voice(env):
    ctx, cal, heads, clock, ntfy, said = env
    await cal.refresh()
    assert heads.context_note() == ("Heads-ups: calendar 30 minutes before, rain/snow coming; said out loud "
                                    "(not 9:30 pm to 7 am) and sent to the phone.")
    res = await heads.run("heads_up_settings", {"weather": False, "calendar_minutes": 15, "quiet_hours": "22:00-06:30"})
    assert res.startswith("ok: changed. Now: calendar 15 minutes before; said out loud (not 10 pm to 6:30 am)")
    assert ctx.settings.get("heads_up.weather") is False and ctx.settings.get("heads_up.calendar_minutes") == 15
    assert "error" in await heads.run("heads_up_settings", {"quiet_hours": "late"})
    res = await heads.run("heads_up_settings", {"quiet_hours": "off", "phone": False})
    assert "said out loud;" not in res and "(not" not in res and "phone" not in res.split("Now:")[1]
    assert (await heads.run("heads_up_settings", {})).startswith("ok: now:")
    assert (await heads.run("heads_up_settings", {"enabled": False})) == "ok: changed. Now: off."
    assert heads.context_note() == "Heads-ups: off."


def test_prompt_and_tools_are_always_there(env):
    ctx, cal, heads, clock, ntfy, said = env
    assert heads.available() and "heads_up_settings" in [t["name"] for t in heads.tools()]
    assert "Heads up:" in heads.prompt_note()
