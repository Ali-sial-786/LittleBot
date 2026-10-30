"""Hand-offs LittleBot answers itself, without asking the text model: the weather, the time, the volume, timers,
and a card's picture full screen or zoomed.

The live voice hands these over as often as anything ("what's the weather like?", "louder", "set a timer for
five minutes"), and each used to cost a text-model request just to call one tool (two, with its answer): in a
real call the weather service answered in 148 ms and the hand-off still took over 5 s. Here the words are matched
(``match``), the tool runs (``answer``: the same RobotToolbox functions the text model would call) and the voice
gets the facts to say in its own words. Only clear, simple requests match; anything else (a place's time, "why
does it rain?", "cancel the egg timer") goes to the text model as before, and so does a request whose tool fails.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from typing import Any, Callable

from littlebot.safety.content import sanitize_user_text

log = logging.getLogger("littlebot.chatgpt.quick")

MAX_WORDS = 16  # longer hand-offs are more than a simple request: the text model reads them

_NOT = re.compile(r"\b(?:don'?t|do not|never|no need|shouldn'?t|stop)\b", re.I)

# ---------------------------------------------------------------- the weather
_WEATHER = re.compile(r"\b(?:weather|forecast|rain(?:ing|y)?|snow(?:ing|y)?|umbrella|sunny|windy|"
                      r"(?:hot|cold|warm|chilly) (?:is it )?(?:outside|out there|out)|temperature (?:outside|out)|"
                      r"how (?:hot|cold|warm) is it(?: outside| out there| out)?)\b", re.I)
# Questions about weather, not for today's weather: "why does it rain?", "what's a rainbow made of?"
_NOT_WEATHER = re.compile(r"^\W*(?:why|how (?:do|does|did|is|are)|what (?:causes|makes|is a|are)|explain|"
                          r"tell me about|is it true)\b|\b(?:history|climate|record|cards?|game|song|story|video)\b",
                          re.I)
_WEEK = re.compile(r"\b(?:week(?:end)?|next (?:few|couple of|\w+) days|(?:\d|three|four|five|six|seven) days|"
                   r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
# "in Paris", "for New York City": a place. "in the morning", "for today", "at home": not one.
_PLACE = re.compile(r"\b(?:in|for|at|near|around)\s+([^\s,.?!]+(?:[ ,]+[^\s,.?!]+){0,3})")
_NOT_PLACE = frozenset("""the a an this that my our your here home today tonight tomorrow now morning afternoon
evening night week weekend later outside out there it me us him her them us kids baby celsius fahrenheit degrees
monday tuesday wednesday thursday friday saturday sunday a bit while next""".split())

# ---------------------------------------------------------------- the time
_TIME = re.compile(r"\b(?:what time|the time|current time|time is it|time it is|what day|which day|day is it|"
                   r"day it is|what date|the date|today'?s date|date is it|what(?:'s| is) today)\b", re.I)
# Not just "now, here": another place, a time span, an alarm ("what time is it in Tokyo?", "how long until 5?")
_NOT_TIME = re.compile(r"\bwhat time (?!is it|it is)|\b(?:in|at|for|until|till|til|since|ago|left|when|how long|how many|zones?|timers?|alarms?|"
                       r"remind\w*|bed\s?time|lunch\s?time|nap\s?time|tea\s?time|dinner\s?time|story\s?time)\b", re.I)

# ---------------------------------------------------------------- the volume
_SOUND = r"(?:it|that|this|the volume|the sound|your volume|your voice|you|yourself|the video|the music)"
_LOUDER = re.compile(rf"\b(?:louder|(?:turn|crank|bring) (?:{_SOUND} )?up(?: {_SOUND})?|volume up|"
                     r"(?:raise|increase|boost) (?:the |your )?volume|speak up|more volume|can'?t hear (?:you|it))\b",
                     re.I)
_QUIETER = re.compile(rf"\b(?:quieter|softer|(?:turn|bring) (?:{_SOUND} )?down(?: {_SOUND})?|volume down|"
                      r"(?:lower|decrease|reduce) (?:the |your )?volume|less loud|too loud|not so loud)\b", re.I)
_A_LOT = re.compile(r"\b(?:a lot|much|way|lots|really|a bunch)\b", re.I)
_SET_VOLUME = re.compile(r"\b(?:volume|sound)\b\D{0,20}?\b(\d{1,3})\s*(?:%|percent)?(?!\s*(?:minutes?|seconds?))|"
                         r"\b(\d{1,3})\s*(?:%|percent)\D{0,12}\b(?:volume|loud)", re.I)
_GET_VOLUME = re.compile(r"\b(?:what(?:'s| is) (?:the|your) volume|how loud (?:are you|is it)|volume level)\b",
                         re.I)
_VOLUME_ISH = re.compile(r"\b(?:volume|loud\w*|quiet\w*|soft\w*|sound|hear)\b|turn (?:\w+ ){0,2}(?:up|down)", re.I)
# "play baby shark louder": a video asked for (the text model plays it), not only the volume
_NOT_VOLUME = re.compile(r"\b(?:play|put on|watch|sing|song|start|search|find|show)\b", re.I)

# ---------------------------------------------------------------- timers
_TIMER = re.compile(r"\b(?:timer|count ?down)\b", re.I)
_NOT_TIMER = re.compile(r"\b(?:cancel|stop|pause|resume|add|more time|left|remaining|how (?:long|much)|delete|"
                        r"remove|reset|end|finish|off)\b", re.I)
_NUMBERS = {w: n for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_NUMBERS.update({"a": 1, "an": 1, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90,
                 "a couple of": 2, "a couple": 2, "a few": 3, "couple of": 2, "few": 3})
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_UNIT = r"(hours?|hrs?|minutes?|mins?|seconds?|secs?)"
_NUMBER = (r"(\d+(?:\.\d+)?|(?:twenty|thirty|forty|fifty)(?:[\s-](?:one|two|three|four|five|six|seven|eight|nine))?|"
           r"a couple(?: of)?|a few|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
           r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|sixty|ninety|an?)")
_DURATION = re.compile(rf"\b{_NUMBER}(?:\s+and\s+a\s+half)?[\s-]*{_UNIT}(\s+and\s+a\s+half)?\b", re.I)
_HALF_HOUR = re.compile(r"\bhalf (?:an|a) hour\b", re.I)
_UNIT_S = {"h": 3600, "m": 60, "s": 1}
_LABELS = (re.compile(r"\b([a-z]+)\s+timer\b", re.I),  # "an egg timer"
           re.compile(r"\btimer\b.*?\bfor\s+(?:the\s+|my\s+|our\s+|her\s+|his\s+)?([a-z]+)\s*[.!?]*$", re.I))
_NOT_LABEL = frozenset("""minute minutes second seconds hour hours min mins sec secs new quick little countdown
another a an the my our one two three four five six seven eight nine ten fifteen twenty thirty forty fifty sixty half few couple
kitchen timer""".split())
MAX_TIMER_S = 24 * 3600


@dataclasses.dataclass(frozen=True)
class Asked:
    """What a hand-off asks for: ``what`` is weather | time | volume | timer | picture; ``args`` the tool's
    arguments."""

    what: str
    args: dict[str, Any]


def match(question: str) -> Asked | None:
    """The simple request ``question`` is, or None (the text model answers it)."""
    text = " ".join(str(question or "").split())
    if not text or len(text.split()) > MAX_WORDS:
        return None
    for find in (_volume, _timer, _picture, _time, _weather):
        asked = find(text)
        if asked is not None:
            return asked
    return None


def _weather(text: str) -> Asked | None:
    if not _WEATHER.search(text) or _NOT_WEATHER.search(text) or _NOT.search(text):
        return None
    place = None
    for m in _PLACE.finditer(text):
        words = m.group(1).split()
        if words[0].lower().strip("'") in _NOT_PLACE:
            continue
        kept = []
        for w in words:
            if not w[:1].isupper() or w.lower() in _NOT_PLACE:
                break
            kept.append(w)
        if not kept:
            return None  # "in paris" (not capitalised): can't tell; the text model reads it
        place = " ".join(kept)
    from littlebot.screens.weather_words import weather_view  # noqa: PLC0415

    view, day = weather_view(text)  # "the hourly": the card goes up hour by hour
    args: dict[str, Any] = {"place": place, "days": 7 if _WEEK.search(text) or view == "week" else 2}
    if view:
        args["view"] = view
    if day:
        args["day"] = day
    return Asked("weather", args)


def _picture(text: str) -> Asked | None:
    """"Show the picture full screen", "zoom in": the card's picture (only done when a card with one is up)."""
    from littlebot.screens.picture_words import picture_view  # noqa: PLC0415

    view = picture_view(text)
    return Asked("picture", {"view": view}) if view else None


def _time(text: str) -> Asked | None:
    if not _TIME.search(text) or _NOT_TIME.search(text):
        return None
    return Asked("time", {})


def _volume(text: str) -> Asked | None:
    if _NOT.search(text) and not re.search(r"\bnot so loud\b", text, re.I):
        return None
    if _NOT_VOLUME.search(text):
        return None
    if not _VOLUME_ISH.search(text) and not re.search(r"\b(?:louder|speak up)\b", text, re.I):
        return None
    m = _SET_VOLUME.search(text)
    if m is not None:
        return Asked("volume", {"action": "set", "percent": min(100, int(m.group(1) or m.group(2)))})
    if _GET_VOLUME.search(text):
        return Asked("volume", {"action": "get"})
    up, down = bool(_LOUDER.search(text)), bool(_QUIETER.search(text))
    if up == down:  # neither, or both ("not louder, quieter"): the text model reads it
        return None
    return Asked("volume", {"action": "up" if up else "down", "step": 20 if _A_LOT.search(text) else 10})


def _timer(text: str) -> Asked | None:
    if not _TIMER.search(text) or _NOT_TIMER.search(text) or _NOT.search(text):
        return None
    seconds = duration_seconds(text)
    if seconds is None or not 1 <= seconds <= MAX_TIMER_S:
        return None
    args: dict[str, Any] = {"seconds": seconds}
    for pattern in _LABELS:
        m = pattern.search(text)
        if m is not None and m.group(1).lower() not in _NOT_LABEL:
            args["label"] = m.group(1).lower()
            break
    return Asked("timer", args)


def duration_seconds(text: str) -> int | None:
    """"five minutes" -> 300, "an hour and a half" -> 5400, "2 minutes 30 seconds" -> 150; None if none."""
    total = 0.0
    found = False
    if _HALF_HOUR.search(text):
        text = _HALF_HOUR.sub(" ", text)
        total, found = 1800.0, True
    for m in _DURATION.finditer(text):
        n = _number(m.group(1))
        if n is None:
            continue
        unit = _UNIT_S[m.group(2)[0].lower()]
        half = 0.5 if (m.group(3) or " and a half" in m.group(0).lower()) else 0.0
        total += (n + half) * unit
        found = True
    return int(round(total)) if found else None


def _number(word: str) -> float | None:
    word = word.lower().replace("-", " ").strip()
    try:
        return float(word)
    except ValueError:
        pass
    if word in _NUMBERS:
        return float(_NUMBERS[word])
    parts = word.split()
    if len(parts) == 2 and parts[0] in _TENS and parts[1] in _NUMBERS:
        return float(_TENS[parts[0]] + _NUMBERS[parts[1]])
    return None


def _say_duration(seconds: int) -> str:
    parts = []
    for unit, name in ((3600, "hour"), (60, "minute"), (1, "second")):
        n, seconds = divmod(seconds, unit)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


async def answer(asked: Asked, box: Any, question: str, *,
                 spawn: Callable[[Any], Any] | None = None) -> str | None:
    """Run ``asked``'s tool on ``box`` (a RobotToolbox) and return what the voice is told, or None when the
    tool couldn't answer (the text model then tries). ``spawn(coro)`` runs the screen's card alongside."""
    asked_for = sanitize_user_text(question, max_chars=200)
    if asked.what == "volume":
        return await _answer_volume(asked.args, box)
    if asked.what == "time":
        return await _answer_time(box)
    if asked.what == "timer":
        return await _answer_timer(asked.args, box)
    if asked.what == "weather":
        return await _answer_weather(asked.args, box, asked_for, spawn)
    if asked.what == "picture":
        return await _answer_picture(asked.args, box)
    return None


def done_note(asked: Asked, question: str, told: str, ago_s: float, *, timers_known: bool = False) -> str:
    """One line for the text model about a quick answer earlier in the call, which it never saw (``told``: what
    the voice was told then, ``ago_s``: how long ago), so a follow-up ("how much time is left on it?") gets a
    right answer instead of one that says no timer was set. ``timers_known``: the note also says what the
    timers are now (the screen service's own count), so no guess at the time left is added here."""
    asked_for = sanitize_user_text(question, max_chars=120)
    ago = int(max(0.0, ago_s))
    when = f"{_say_duration(ago)} ago" if ago >= 1 else "just now"
    if asked.what == "timer" and told.startswith("Done:"):
        seconds = int(asked.args["seconds"])
        label = f" for the {asked.args['label']}" if asked.args.get("label") else ""
        started = f'"{asked_for}": LittleBot started a timer for {_say_duration(seconds)}{label} on its screen {when}'
        if timers_known:
            return started + "."
        left = seconds - ago
        state = f"about {_say_duration(left)} left" if left > 0 else "it has finished and chimed"
        return f"{started} ({state}, unless someone stopped it)."
    if asked.what == "weather":
        return f'"{asked_for}": LittleBot told them the weather from its weather service {when}.'
    said = told.split(" Tell them", 1)[0].split(" Say so", 1)[0].strip()
    return f'"{asked_for}": LittleBot answered {when}: {said}'


async def _answer_picture(args: dict[str, Any], box: Any) -> str | None:
    result = await box.run("change_card", {"action": "picture", "args": {"view": args["view"]}})
    if not result.startswith("ok:"):
        return None  # no card with a picture up: the text model says what it can
    return f"Done: {result[3:].strip()}"


async def _answer_volume(args: dict[str, Any], box: Any) -> str | None:
    action = args.get("action")
    if action == "get":
        return f"LittleBot's volume, in percent: {await box.run('get_volume', {})}. Tell them in a few words."
    if action == "set":
        result = await box.run("set_volume", {"percent": args["percent"]})
    else:
        result = await box.run("volume_up" if action == "up" else "volume_down", {"step": args.get("step", 10)})
    if not result.startswith("ok:"):
        return None
    return f"Done: LittleBot's {result[3:].strip()}. Tell them in a few words."


async def _answer_time(box: Any) -> str | None:
    result = await box.run("get_time", {})
    try:
        now = json.loads(result)
    except ValueError:
        return None
    return (f"It is {now['time_12h']} on {now['date']} here ({now['part_of_day']}). Tell them in a few words, the "
            f"way they asked.")


async def _answer_timer(args: dict[str, Any], box: Any) -> str | None:
    if not box.screen_cards():
        return None  # no cards on this screen: the text model says what it can
    spec = {"card": "timer", "fields": {k: v for k, v in args.items()}}
    result = await box.run("show_on_screen", spec)
    label = f" (for the {args['label']})" if args.get("label") else ""
    if result.startswith("ok:"):
        return (f"Done: a timer for {_say_duration(int(args['seconds']))}{label} is counting down on LittleBot's "
                f"screen; it chimes when it's done. Tell them in a few words.")
    if "busy" in result:
        return "LittleBot's screen is busy (a game or a video), so no timer was set. Say so briefly."
    return None


async def _answer_weather(args: dict[str, Any], box: Any, question: str,
                          spawn: Callable[[Any], Any] | None) -> str | None:
    from littlebot.info.weather import WeatherError, get_weather  # noqa: PLC0415

    place, days = args.get("place"), int(args.get("days") or 2)
    if args.get("day") not in (None, "today", "tomorrow"):
        days = 7  # a weekday: the forecast has to reach it
    card = box.weather_card(place, days, view=args.get("view"), day=args.get("day")) \
        if spawn is not None and box.screen_cards() else None
    if card is not None:
        spawn(card)  # the forecast card goes up while the voice says it (not after)
    try:
        report = await get_weather(box.ctx).report(place, days=days)
    except WeatherError as e:
        log.info("quick answer: the weather service didn't answer (%s); the text model tries", e)
        return None
    box.calls.append("get_weather")
    data = json.dumps(_compact(report), ensure_ascii=False, separators=(",", ":"), default=str)
    return (f"Answer what they asked (\"{question}\") from the weather service's answer, in one or two short spoken "
            f"sentences: what it is like, the temperature and whether to expect rain or snow; rounded numbers, no "
            f"lists. The weather service says: {data}")


_NOW_KEYS = ("temperature", "feels_like", "conditions", "wind")
_DAY_KEYS = ("day", "conditions", "high", "low", "chance_of_rain_or_snow_percent", "wind_max")


def _compact(report: dict[str, Any]) -> dict[str, Any]:
    """The parts of a weather report the voice needs (the voice's answer has room for 1800 characters, and a
    week's full report is longer)."""
    out: dict[str, Any] = {k: report[k] for k in ("place", "units") if k in report}
    now = report.get("now")
    if isinstance(now, dict):
        out["now"] = {k: now[k] for k in _NOW_KEYS if k in now}
    days = report.get("forecast")
    if isinstance(days, list):
        out["forecast"] = [{k: d[k] for k in _DAY_KEYS if k in d} for d in days if isinstance(d, dict)]
    return out


async def run_with_timeout(asked: Asked, box: Any, question: str, timeout: float, *,
                           spawn: Callable[[Any], Any] | None = None) -> str | None:
    """``answer`` within ``timeout`` seconds, else None. Never raises (except cancellation)."""
    try:
        return await asyncio.wait_for(answer(asked, box, question, spawn=spawn), timeout)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        log.info("quick answer: %s took over %.1f s; the text model answers", asked.what, timeout)
    except Exception:  # noqa: BLE001 - the text model is always there to answer
        log.warning("quick answer: %s failed", asked.what, exc_info=True)
    return None


__all__ = ["Asked", "answer", "done_note", "duration_seconds", "match", "run_with_timeout"]
