"""Briefing: "good morning", "what's my day", "brief me", "good night", "what's the news".

``get_briefing(kind)`` gathers, in one go, what a morning (or evening) summary needs and hands it to the brain as
compact JSON to say in its own warm words: the local time and a greeting, the weather at home (today's high, low,
conditions and chance of rain; the evening one is tomorrow's), what the reminders and lists skills hold (their
``briefing_note()`` when they have one, else their ``context_note()``) and the top headlines from the RSS feeds
(``skills.news_feeds``, littlebot/skills/news.py). The weather and the news are fetched together, each with its own
time limit, so the briefing is never slower than the slower of the two (a few seconds at worst, cached after).
A briefing card goes up on the screen meanwhile (littlebot/screens/cards/briefing.py): the date, the weather line,
three headlines and the next reminder.

``get_headlines(topic, count)``: more news, or news about something ("any news about the Leafs?").

The context note says when today's morning briefing was already given (kept in memory only), so a second
"good morning" gets a shorter hello instead of the whole thing again.

Speed: in the hours a briefing is likely (``WARM_HOURS``: early morning and the evening) the headlines and home's
forecast are fetched ahead every few minutes in the background (only while cloud communication is allowed), so "good
morning" answers from the cache at once. Nothing about the family is sent: plain GETs of the feeds and the forecast.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
import time
from typing import Any, Callable

from littlebot.skills.base import INT, STR, Skill, ToolSpec, as_json, err, ok, tool
from littlebot.skills.news import NewsError, NewsFeeds

log = logging.getLogger("littlebot.skills.briefing")

KINDS = ("morning", "evening", "news")
CARD_HEADLINES = 3
BRIEF_HEADLINES = 3  # in a morning / evening briefing (the "news" kind gives skills.news_items)
CARD_WAIT_S = 0.5  # the card goes up while the answer is written; this is the most the tool waits for it
NOTE_CHARS = 400  # of another skill's note, in the briefing
WARM_HOURS = frozenset({5, 6, 7, 8, 9, 10, 17, 18, 19, 20, 21, 22})  # local hours the cache is kept warm
WARM_FIRST_S = 90.0  # after start (the robot settles first)
WARM_EVERY_S = 9 * 60.0  # within the forecast's 10 minutes (info.cache_minutes) and the news' 15

SAY = {
    "morning": "Turn this into a warm, natural 20-40 second spoken morning briefing: the greeting, the weather in "
               "plain words (what to wear or bring), what's on today (reminders, alarms) and anything on the lists "
               "worth a mention, then the top 2-3 headlines briefly with their source, and one friendly closing "
               "line. Skip parts that are empty; never read it out like a list.",
    "evening": "Turn this into a calm 20-40 second spoken evening wind-down: tomorrow's weather, what is set for "
               "tomorrow (alarms, reminders), a quick look at the lists if useful, two headlines at most, and a warm "
               "good night. Skip parts that are empty.",
    "news": "Say the top headlines naturally (about 20-40 seconds), each in a sentence with its source in passing; "
            "offer more on any of them.",
}


class Briefing(Skill):
    name = "briefing"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time, news: NewsFeeds | None = None) -> None:
        super().__init__(ctx, now=now)
        self.news = news or NewsFeeds(ctx)
        self._given: dict[str, tuple[str, str]] = {}  # kind -> (local date, local time) it was last given
        self._warmer: asyncio.Task[Any] | None = None

    # ------------------------------------------------------------ the skill
    def available(self) -> bool:
        return bool(getattr(self.ctx.config.skills, "briefing", True))

    def tools(self) -> list[ToolSpec]:
        return [
            tool("get_briefing", "Everything for a spoken briefing in one call: the local time and a greeting, the "
                 "weather at home (today's for morning, tomorrow's for evening), today's reminders and alarms, the "
                 "lists, and the top news headlines. Call it when the user says good morning, good night, 'what's "
                 "my day (look like)', 'brief me', 'what did I miss' or 'what's the news'.",
                 {"kind": {"type": "string", "enum": list(KINDS),
                           "description": "morning (good morning, what's my day), evening (good night, how does "
                                          "tomorrow look) or news (just the headlines)"}}),
            tool("get_headlines", "The latest news headlines (titles and their source) from LittleBot's news feeds, "
                 "optionally only those about a topic.",
                 {"topic": {**STR, "description": "optional: a few words, e.g. 'hamilton', 'election', 'hockey'"},
                  "count": {**INT, "minimum": 1, "maximum": 15, "description": "how many, default 5"}}),
        ]

    def prompt_note(self) -> str:
        return ("Briefings: when the user says good morning, good night, 'what's my day', 'brief me' or 'what's the "
                "news', call get_briefing (kind morning, evening or news) and give a warm 20-40 second spoken "
                "summary from it in your own words (a card with the gist goes on the screen by itself). For more "
                "news or news about something, get_headlines.")

    def context_note(self) -> str:
        today = self._local().date().isoformat()
        given = self._given.get("morning")
        if given and given[0] == today:
            return (f"Briefing: today's morning briefing was already given (at {given[1]}); if asked again, keep it "
                    f"short and say only what's new.")
        return ""

    def status(self) -> dict[str, Any]:
        return {**super().status(), "feeds": len(self.news.feeds()), "given": dict(self._given)}

    async def start(self) -> None:
        if self.available():
            self._warmer = asyncio.get_running_loop().create_task(self._keep_warm())

    async def stop(self) -> None:
        if self._warmer is not None:
            self._warmer.cancel()
            try:
                await self._warmer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._warmer = None
        await self.news.aclose()

    async def _keep_warm(self) -> None:
        """Fetch the headlines and the forecast ahead in the hours a briefing is likely (never raises)."""
        await asyncio.sleep(WARM_FIRST_S)
        while True:
            await self.warm()
            await asyncio.sleep(WARM_EVERY_S)

    async def warm(self) -> bool:
        """One warm-up, when it is a likely hour and the network may be used. True when it fetched."""
        if self._local().hour not in WARM_HOURS or self.news.blocked_reason() is not None:
            return False
        from littlebot.info.weather import get_weather  # noqa: PLC0415

        weather = get_weather(self.ctx)
        jobs = [self._news(BRIEF_HEADLINES)]
        if getattr(weather, "blocked_reason", lambda: None)() is None:
            jobs.append(self._weather(0))
        await asyncio.gather(*jobs, return_exceptions=True)
        log.debug("briefing: cache warmed")
        return True

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if name == "get_briefing":
            return await self._briefing(args)
        if name == "get_headlines":
            return await self._headlines(args)
        return await super().run(name, args)

    # ------------------------------------------------------------ get_briefing
    async def _briefing(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind") or "").strip().lower()
        local = self._local()
        if kind not in KINDS:
            kind = "morning" if 4 <= local.hour < 16 else "evening"
        clock = self._clock(local)
        out: dict[str, Any] = {"kind": kind, "greeting": greeting(kind, local.hour), "time": clock["time_12h"],
                               "date": clock["date"]}
        count = int(self.ctx.config.skills.news_items) if kind == "news" else BRIEF_HEADLINES
        if kind == "news":
            weather_task = None
        else:
            weather_task = asyncio.ensure_future(self._weather(1 if kind == "evening" else 0))
        news_task = asyncio.ensure_future(self._news(count))
        weather = await weather_task if weather_task is not None else None
        headlines, news_problem = await news_task
        if weather is not None:
            out["weather"] = weather
        if kind != "news":
            for skill_name in ("reminders", "lists"):
                note = self._note(skill_name)
                if note:
                    out[skill_name] = note
        if headlines:
            out["headlines"] = [h.to_dict() for h in headlines]
        elif news_problem:
            out["headlines_unavailable"] = news_problem
        out["say"] = SAY[kind]
        self._given[kind] = (local.date().isoformat(), clock["time_12h"])
        await self._show_card(kind, out, local)
        log.info("briefing: %s (weather %s, %d headlines)", kind, "yes" if weather and "unavailable" not in weather
                 else "no", len(headlines))
        return as_json(out)

    def _local(self) -> _dt.datetime:
        from littlebot.info.clock import zone  # noqa: PLC0415

        try:
            tz = zone(getattr(self.ctx.config.info, "timezone", "") or None)
        except ValueError:
            tz = None
        base = _dt.datetime.fromtimestamp(float(self.now()), tz=_dt.timezone.utc)
        return base.astimezone(tz) if tz is not None else base.astimezone()

    def _clock(self, local: _dt.datetime) -> dict[str, Any]:
        from littlebot.info.clock import now_info  # noqa: PLC0415

        try:
            return now_info(getattr(self.ctx.config.info, "timezone", "") or None, now=local)
        except ValueError:
            return now_info(None, now=local)

    async def _weather(self, day: int) -> dict[str, Any]:
        """Home's weather for the briefing (day 0 today with now, 1 tomorrow), or {"unavailable": why}."""
        from littlebot.info.weather import WeatherError, get_weather  # noqa: PLC0415

        try:
            report = await get_weather(self.ctx).report(None, days=2)
        except WeatherError as e:
            return {"unavailable": str(e)}
        except Exception:  # noqa: BLE001 - the briefing goes on without it
            log.warning("briefing: weather failed", exc_info=True)
            return {"unavailable": "the weather service didn't work"}
        return weather_summary(report, day)

    async def _news(self, count: int) -> tuple[list[Any], str | None]:
        try:
            return await self.news.headlines(count), None
        except NewsError as e:
            return [], str(e)
        except Exception:  # noqa: BLE001
            log.warning("briefing: news failed", exc_info=True)
            return [], "the news feeds didn't work"

    def _note(self, skill_name: str) -> str:
        """What another skill (reminders, lists) holds right now, for the briefing; "" when it isn't there."""
        skill = self._skill(skill_name)
        if skill is None:
            return ""
        for attr in ("briefing_note", "context_note"):
            fn = getattr(skill, attr, None)
            if not callable(fn):
                continue
            try:
                text = str(fn() or "").strip()
            except Exception:  # noqa: BLE001 - another skill's trouble is not the briefing's
                log.debug("briefing: %s.%s failed", skill_name, attr, exc_info=True)
                continue
            if text:
                return text[:NOTE_CHARS]
        return ""

    def _skill(self, skill_name: str) -> Any:
        hub = getattr(self.ctx, "skills", None)
        get = getattr(hub, "get", None)
        if get is None:
            return None
        try:
            skill = get(skill_name)
        except Exception:  # noqa: BLE001
            return None
        try:
            if skill is not None and hasattr(skill, "available") and not skill.available():
                return None
        except Exception:  # noqa: BLE001
            return None
        return skill

    def _next_reminder(self) -> str:
        """One short line for the card: the reminders skill's ``next_reminder()`` if it has one, else the first
        item of its note ("Reminders: 6 pm call mom; ..." -> "6 pm call mom")."""
        skill = self._skill("reminders")
        if skill is None:
            return ""
        fn = getattr(skill, "next_reminder", None)
        if callable(fn):
            try:
                value = fn()
            except Exception:  # noqa: BLE001
                value = None
            if isinstance(value, dict):
                value = " ".join(str(value.get(k) or "") for k in ("when", "text")).strip()
            if value:
                return str(value)[:80]
        return first_item(self._note("reminders"))[:80]

    async def _show_card(self, kind: str, data: dict[str, Any], local: _dt.datetime) -> None:
        if getattr(self.ctx, "screens", None) is None:
            return
        spec = card_spec(kind, data, local, self._next_reminder())
        task = asyncio.ensure_future(self.show(spec))
        try:
            await asyncio.wait_for(asyncio.shield(task), CARD_WAIT_S)
        except asyncio.TimeoutError:
            pass  # it carries on and goes up by itself

    # ------------------------------------------------------------ get_headlines
    async def _headlines(self, args: dict[str, Any]) -> str:
        topic = " ".join(str(args.get("topic") or "").split())[:60]
        try:
            count = int(args.get("count") or self.ctx.config.skills.news_items)
        except (TypeError, ValueError):
            count = int(self.ctx.config.skills.news_items)
        count = max(1, min(15, count))
        try:
            found = await self.news.headlines(count, topic or None)
        except NewsError as e:
            return err(str(e))
        if not found:
            return ok(f"no headlines about {topic} in the news feeds right now" if topic else "no headlines right now")
        out: dict[str, Any] = {"headlines": [h.to_dict() for h in found]}
        if topic:
            out["topic"] = topic
        return as_json(out)


# ---------------------------------------------------------------- helpers
def greeting(kind: str, hour: int) -> str:
    if kind == "evening":
        return "Good night" if hour >= 21 or hour < 4 else "Good evening"
    if 4 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 17:
        return "Good afternoon"
    return "Good evening"


def weather_summary(report: dict[str, Any], day: int) -> dict[str, Any]:
    """The weather report (littlebot/info/weather.py) cut down to what a briefing says."""
    units = (report.get("units") or {}).get("temperature") or "°"
    forecast = [d for d in report.get("forecast") or [] if isinstance(d, dict)]
    d = forecast[day] if 0 <= day < len(forecast) else (forecast[0] if forecast else {})
    out: dict[str, Any] = {"place": str(report.get("place") or "").split(",")[0].strip() or "home", "units": units,
                           "day": d.get("day") or ("today" if day == 0 else "tomorrow")}
    for key in ("high", "low", "conditions", "chance_of_rain_or_snow_percent", "sunset"):
        if d.get(key) is not None:
            out[key] = d[key]
    now = report.get("now") or {}
    if day == 0 and now:
        out["now"] = {k: now[k] for k in ("temperature", "feels_like", "conditions") if now.get(k) is not None}
    return out


_ITEM_SPLIT = re.compile(r"\s*(?:;|\n|\. )\s*")
_LABEL = re.compile(r"^[A-Za-z][A-Za-z ]{0,30}:\s+")


def first_item(note: str) -> str:
    """ "Reminders: 6 pm call mom; 8 pm bins" -> "6 pm call mom"."""
    s = _LABEL.sub("", str(note or "").strip(), count=1)
    parts = [p.strip(" .,-") for p in _ITEM_SPLIT.split(s) if p.strip(" .,-")]
    return parts[0] if parts else ""


def weather_line(w: dict[str, Any] | None) -> str:
    """ "12° / 4° · light rain · rain 70%" for the card ("" without a forecast)."""
    if not w or "unavailable" in w:
        return ""
    parts = []
    if w.get("high") is not None and w.get("low") is not None:
        parts.append(f"{w['high']}° / {w['low']}°")
    if w.get("conditions"):
        parts.append(str(w["conditions"]))
    chance = w.get("chance_of_rain_or_snow_percent")
    if isinstance(chance, int) and chance >= 20:
        parts.append(f"rain {chance}%")
    return " · ".join(parts)


def card_spec(kind: str, data: dict[str, Any], local: _dt.datetime, reminder: str = "") -> dict[str, Any]:
    weather = data.get("weather") if isinstance(data.get("weather"), dict) else None
    title = {"news": "Top stories", "evening": data.get("greeting") or "Good evening"}.get(
        kind, data.get("greeting") or "Good morning")
    spec: dict[str, Any] = {"card": "briefing", "title": title, "date": f"{local:%A} {local.day} {local:%B}"}
    line = weather_line(weather)
    if line:
        spec["weather"] = ("Tomorrow: " if kind == "evening" else "") + line
        spec["conditions"] = str((weather or {}).get("conditions") or "")
        now = (weather or {}).get("now") or {}
        if now.get("temperature") is not None:
            spec["temperature"] = now["temperature"]
    heads = [h.get("title") for h in data.get("headlines") or [] if isinstance(h, dict) and h.get("title")]
    sources = [h.get("source") or "" for h in data.get("headlines") or [] if isinstance(h, dict) and h.get("title")]
    if heads:
        n = len(heads) if kind == "news" else CARD_HEADLINES
        spec["headlines"] = heads[:n]
        spec["sources"] = sources[:n]
    if reminder and kind != "news":
        spec["reminder"] = reminder
    return spec


__all__ = ["Briefing", "card_spec", "first_item", "greeting", "weather_line", "weather_summary"]
