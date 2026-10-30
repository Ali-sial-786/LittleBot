"""Natural times for reminders and alarms: "in 20 minutes", "at 6pm", "tomorrow at 7:30", "every weekday at 7".

The text model usually sends an ISO-8601 local time ("2026-10-30T18:00"); the phrases people say are understood
too, so a model that passes the words on still gets the right time. Everything is resolved in the home's time
zone (``info.timezone``, else the robot's own) and the result says the exact time back, so the voice can confirm
it ("6 pm today, Friday 30 October").

* :func:`parse_when` -> :class:`When` (the first time it is due and the weekdays it repeats on), or
  :class:`WhenError` with a message the model can act on.
* :func:`parse_repeat` reads a separate repeat value ("daily", "weekdays", "weekly", "monday,friday").
* :func:`next_time` is the next local time of day on one of the weekdays (daylight-saving safe: every candidate
  is built as a wall-clock time and placed in the zone).
* :func:`say_when`, :func:`clock_words`, :func:`until_words`, :func:`repeat_words` say it back.

No dependencies beyond the standard library.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Literal

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DAY_NAMES = {name: i for i, name in enumerate(WEEKDAYS)}
_DAY_NAMES.update({"mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4,
                   "sat": 5, "sun": 6})
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
_MONTH_NAMES = {name: i + 1 for i, name in enumerate(MONTHS)}
_MONTH_NAMES.update({m[:3]: i + 1 for i, m in enumerate(MONTHS)})
_MONTH_NAMES.update({"sept": 9})

EVERY_DAY = frozenset(range(7))
WEEKDAY_SET = frozenset(range(5))
WEEKEND_SET = frozenset({5, 6})
MAX_AHEAD = _dt.timedelta(days=366)
# A day said without a time: these local times (a reminder; an alarm always needs its time).
DAYPART_DEFAULT = {"morning": (8, 0), "afternoon": (14, 0), "evening": (18, 0), "night": (20, 0)}
DAY_DEFAULT = (9, 0)

Repeat = frozenset[int] | Literal["weekly"] | None

HINT = ("give an ISO-8601 local time like 2026-10-30T18:00, or say it like 'in 20 minutes', 'at 6pm', "
        "'tomorrow at 7:30', 'Friday at 5pm' or 'every weekday at 7'")


class WhenError(ValueError):
    """The time couldn't be understood (or has passed): ``str(e)`` says why, for the model."""


@dataclass(frozen=True)
class When:
    at: _dt.datetime  # the first time it is due (aware, in the home's zone)
    days: frozenset[int] = frozenset()  # repeats on these weekdays (0 = Monday); empty: once

    @property
    def hour(self) -> int:
        return self.at.hour

    @property
    def minute(self) -> int:
        return self.at.minute


# ---------------------------------------------------------------- zones and wall-clock times
def local_dt(day: _dt.date, hour: int, minute: int, tz: _dt.tzinfo | None, second: int = 0) -> _dt.datetime:
    """``day`` at ``hour:minute`` on the wall clock in ``tz`` (None: the robot's own zone), as an aware datetime."""
    naive = _dt.datetime(day.year, day.month, day.day, hour, minute, second)
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def now_local(ts: float, tz: _dt.tzinfo | None) -> _dt.datetime:
    """Wall-clock seconds ``ts`` as an aware datetime in ``tz`` (None: the robot's own zone)."""
    return _dt.datetime.fromtimestamp(ts, tz) if tz is not None else _dt.datetime.fromtimestamp(ts).astimezone()


def zone_of(at: _dt.datetime) -> _dt.tzinfo | None:
    """``at``'s named zone (a ZoneInfo), or None for a fixed offset (the robot's own zone, read by astimezone())."""
    tz = at.tzinfo
    return tz if tz is not None and not isinstance(tz, _dt.timezone) else None


def next_time(after: _dt.datetime, hour: int, minute: int, days: frozenset[int] | set[int] = frozenset(),
              tz: _dt.tzinfo | None = None) -> _dt.datetime:
    """The first ``hour:minute`` strictly after ``after`` on one of ``days`` (empty: any day), in ``tz`` (None:
    ``after``'s own zone, else the robot's)."""
    tz = tz if tz is not None else zone_of(after)
    start = after.astimezone(tz).date() if tz is not None else after.astimezone().date()
    for i in range(9):
        day = start + _dt.timedelta(days=i)
        if days and day.weekday() not in days:
            continue
        cand = local_dt(day, hour, minute, tz)
        if cand > after:
            return cand
    raise WhenError("no such day")  # (days was empty-handed: never with a valid weekday set)


# ---------------------------------------------------------------- saying it back
def clock_words(at: _dt.datetime, twelve: bool = True) -> str:
    """ "6 pm", "6:30 pm", "12 noon"-free "12 pm"; "18:30" in 24 hours."""
    if not twelve:
        return f"{at.hour:02d}:{at.minute:02d}"
    h = at.hour % 12 or 12
    ampm = "am" if at.hour < 12 else "pm"
    return f"{h} {ampm}" if at.minute == 0 else f"{h}:{at.minute:02d} {ampm}"


def day_words(at: _dt.datetime, now: _dt.datetime) -> str:
    """ "today", "tomorrow", "on Friday" (this week), "on Friday 6 November" (later), with the year when it differs."""
    d, today = at.date(), now.astimezone(at.tzinfo).date()
    delta = (d - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta < 7:
        return f"on {at:%A}"
    year = f" {d.year}" if d.year != today.year else ""
    return f"on {at:%A} {d.day} {at:%B}{year}"


def say_when(at: _dt.datetime, now: _dt.datetime, twelve: bool = True) -> str:
    """ "6 pm today (Friday 30 October)", "7:30 am tomorrow (Saturday 31 October)", "5 pm on Friday 6 November"."""
    day = day_words(at, now)
    full = f"{at:%A} {at.day} {at:%B}"
    if day in ("today", "tomorrow") or (day.startswith("on ") and day.count(" ") == 1):
        return f"{clock_words(at, twelve)} {day} ({full})"
    return f"{clock_words(at, twelve)} {day}"


def until_words(seconds: float) -> str:
    """ "in 45 seconds", "in 20 minutes", "in 2 hours 10 minutes", "in 3 days"."""
    s = max(0, round(seconds))
    if s < 60:
        return f"in {s} second{'s' if s != 1 else ''}"
    if s < 300 and s % 60:
        m, sec = divmod(s, 60)
        return f"in {m} minute{'s' if m != 1 else ''} {sec} second{'s' if sec != 1 else ''}"
    if s < 2 * 86400:
        m = (s + 30) // 60
        h, m = divmod(m, 60)
        parts = [f"{n} {unit}{'s' if n != 1 else ''}" for n, unit in ((h, "hour"), (m, "minute")) if n]
        return "in " + " ".join(parts)
    d = round(s / 86400)
    return f"in {d} days"


def repeat_words(days: frozenset[int] | set[int]) -> str:
    """ "every day", "every weekday", "every weekend", "every Monday", "every Monday and Friday"; "" for once."""
    days = frozenset(days)
    if not days:
        return ""
    if days == EVERY_DAY:
        return "every day"
    if days == WEEKDAY_SET:
        return "every weekday"
    if days == WEEKEND_SET:
        return "every weekend"
    names = [WEEKDAYS[d].capitalize() for d in sorted(days)]
    if len(names) == 1:
        return f"every {names[0]}"
    return "every " + ", ".join(names[:-1]) + " and " + names[-1]


def days_text(days: frozenset[int] | set[int]) -> str:
    """For storage: "0,1,2,3,4" ("" once)."""
    return ",".join(str(d) for d in sorted(days))


def days_from_text(text: str | None) -> frozenset[int]:
    out = set()
    for part in str(text or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6:
            out.add(int(part))
    return frozenset(out)


# ---------------------------------------------------------------- reading the words
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90}
_UNIT_WORD = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
            "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400,
            "day": 86400, "days": 86400, "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800}
_AMOUNT = r"\d+(?:\.\d+)?"
_REL_UNIT = r"(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|hr|h|days?|d|weeks?|wks?|wk|w)"
_REL_PART = rf"{_AMOUNT}\s*{_REL_UNIT}(?![a-z])"
_REL_CHAIN = rf"{_REL_PART}(?:\s*(?:,|and)?\s*{_REL_PART})*"
_DAY_RE = "|".join(sorted(_DAY_NAMES, key=len, reverse=True))
_MONTH_RE = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))


def _numbers(text: str) -> str:
    """Number words to digits ("twenty five minutes" -> "25 minutes", "an hour and a half" -> "90 minutes")."""
    s = text
    s = re.sub(r"\b(?:a\s+)?quarter\s+of\s+an\s+hour\b|\bquarter\s+hour\b", "15 minutes", s)
    s = re.sub(r"\b(?:a\s+)?half\s+(?:an\s+)?hour\b", "30 minutes", s)
    s = re.sub(r"\b(?:a|an)\s+(?=" + _UNIT_WORD + r"\b)", "1 ", s)
    s = re.sub(r"\ba\s+couple(?:\s+of)?\s+", "2 ", s)
    s = re.sub(r"\ba\s+few\s+", "3 ", s)
    tens = "|".join(_TENS)
    units = "|".join(sorted(_UNITS, key=len, reverse=True))
    s = re.sub(rf"\b({tens})[\s-]+({units})\b",
               lambda m: str(_TENS[m.group(1)] + _UNITS[m.group(2)]) if _UNITS[m.group(2)] < 10 else m.group(0), s)
    s = re.sub(rf"\b({tens})\b", lambda m: str(_TENS[m.group(1)]), s)
    s = re.sub(rf"\b({units})\b", lambda m: str(_UNITS[m.group(1)]), s)
    s = re.sub(r"\b(\d+)\s+(?:and\s+)?a\s+half\s+(" + _UNIT_WORD + r")\b", lambda m: f"{m.group(1)}.5 {m.group(2)}", s)
    s = re.sub(r"\b(\d+)\s+(hours?|days?|weeks?|minutes?)\s+and\s+a\s+half\b",
               lambda m: f"{m.group(1)}.5 {m.group(2)}", s)
    s = re.sub(r"\b(\d{1,2})\s+oh\s+(\d)\b", r"\1:0\2", s)  # "seven oh five" -> 7:05
    return s


def _clean(text: str) -> str:
    s = " ".join(str(text or "").lower().split())
    s = s.replace("a.m.", "am").replace("p.m.", "pm").replace("’", "'")
    s = re.sub(r"\b(\d{1,2})\s*([ap])\.?\s*m\b\.?", r"\1 \2m", s)
    s = re.sub(r"\b(\d{1,2}):(\d{2})\s*([ap])\.?m\b", r"\1:\2 \3m", s)
    s = re.sub(r"\bo'?\s?clock\b", "", s)
    s = re.sub(r"\b(?:please|remind me|set an alarm|an alarm|alarm|wake me(?: up)?)\b", " ", s)
    s = re.sub(r"\b(tomorow|tommorow|tommorrow|tmrw|tmr)\b", "tomorrow", s)
    s = re.sub(r"\b(?:to|tonite)night\b|\btonite\b", "tonight", s)
    s = s.strip(" .!?,;")
    return " ".join(_numbers(s).split())


@dataclass
class _Parts:
    rel: float | None = None  # seconds from now
    rel_day_unit: bool = False  # the relative part was in days / weeks (a time of day may still be given)
    day_offset: int | None = None  # 0 today, 1 tomorrow ...
    weekday: int | None = None
    next_week: bool = False  # "next friday"
    date: tuple[int | None, int, int] | None = None  # (year or None, month, day)
    hour: int | None = None
    minute: int = 0
    ampm: str | None = None  # "am" | "pm" | "24" (a 24-hour time)
    midnight: bool = False
    daypart: str | None = None  # morning | afternoon | evening | night
    repeat: Repeat = None


def _take(pattern: str, s: str) -> tuple[re.Match[str] | None, str]:
    m = re.search(pattern, s)
    if m is None:
        return None, s
    return m, (s[:m.start()] + " " + s[m.end():]).strip()


def _repeat_phrase(s: str, p: _Parts) -> str:
    """ "every weekday", "daily", "every monday and wednesday", "on weekends", "mondays" -> ``p.repeat``."""
    m, s2 = _take(r"\b(?:every|each)\s+(?:single\s+)?(day|morning|afternoon|evening|night)\b|\b(daily|everyday|"
                  r"nightly)\b", s)
    if m:
        word = m.group(1) or m.group(2)
        if word in ("morning", "afternoon", "evening", "night", "nightly"):
            p.daypart = "night" if word == "nightly" else word
        p.repeat = EVERY_DAY
        return s2
    m, s2 = _take(r"\b(?:every|each)\s+(?:week\s?day|work\s?day)s?\b|\b(?:on\s+)?(?:week\s?days|work\s?days)\b", s)
    if m:
        p.repeat = WEEKDAY_SET
        return s2
    m, s2 = _take(r"\b(?:every|each)\s+weekends?\b|\b(?:on\s+)?weekends\b", s)
    if m:
        p.repeat = WEEKEND_SET
        return s2
    m, s2 = _take(rf"\b(?:every|each)\s+((?:(?:{_DAY_RE})s?\b(?:\s*(?:,|and|&)\s*)?)+)", s)
    if m:
        p.repeat = frozenset(_day_number(w) for w in re.findall(rf"\b(?:{_DAY_RE})s?\b", m.group(1)))
        return s2
    m, s2 = _take(r"\b(?:every|each)\s+week\b|\b(?:once\s+a\s+week|weekly)\b", s)
    if m:
        p.repeat = "weekly"
        return s2
    plural = "|".join(f"{d}s" for d in WEEKDAYS)
    m, s2 = _take(rf"\b(?:on\s+)?((?:(?:{plural})\b(?:\s*(?:,|and|&)\s*)?)+)", s)
    if m:  # "mondays and thursdays": every one of them
        p.repeat = frozenset(_day_number(w) for w in re.findall(rf"\b(?:{plural})\b", m.group(1)))
        return s2
    if re.search(r"\b(?:every|each)\s+(?:other|second|hour|month|year|\d)|\b(?:hourly|monthly|yearly|annually)\b",
                 s):
        raise WhenError("I can repeat daily, on weekdays, on weekends, weekly or on chosen days of the week")
    return s


def _day_number(word: str) -> int:
    return _DAY_NAMES[word] if word in _DAY_NAMES else _DAY_NAMES[word[:-1]]


def _relative(s: str, p: _Parts) -> str:
    m, s2 = _take(rf"\b(?:in|after|within)\s+({_REL_CHAIN})", s)
    if m is None:
        m, s2 = _take(rf"\b({_REL_CHAIN})\s+(?:from\s+now|later|time)\b", s)
    if m is None:
        m2 = re.fullmatch(rf"\s*({_REL_CHAIN})\s*", s)  # just "20 minutes"
        if m2:
            m, s2 = m2, ""
    if m is None:
        return s
    total, day_unit = 0.0, False
    for amount, unit in re.findall(rf"({_AMOUNT})\s*({_REL_UNIT})(?![a-z])", m.group(1)):
        k = _SECONDS[unit]
        total += float(amount) * k
        day_unit = day_unit or k >= 86400
    p.rel, p.rel_day_unit = total, day_unit
    return s2


def _day(s: str, p: _Parts) -> str:
    m, s = _take(r"\b(?:the\s+)?day\s+after\s+tomorrow\b", s)
    if m:
        p.day_offset = 2
    m, s = _take(r"\btomorrow\b", s)
    if m:
        p.day_offset = 1
    m, s = _take(r"\btonight\b", s)
    if m:
        p.day_offset = p.day_offset if p.day_offset is not None else 0
        p.daypart = "night"
    m, s = _take(r"\btoday\b", s)
    if m:
        p.day_offset = 0
    m, s = _take(r"\b(this|next)\s+weekend\b", s)
    if m:
        p.weekday, p.next_week = 5, m.group(1) == "next"
    m, s = _take(r"\bnext\s+week\b", s)
    if m:
        p.day_offset = 7
    m, s = _take(r"\b(?:(?:in\s+the|this|on)\s+)?(morning|afternoon|evening|night)\b", s)
    if m:
        p.daypart = m.group(1)
        if m.group(0).startswith("this"):
            p.day_offset = p.day_offset if p.day_offset is not None else 0
    m, s = _take(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", s)
    if m:
        p.date = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m, s = _take(rf"\b(?:on\s+)?(?:the\s+)?({_MONTH_RE})\.?\s+(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\b"
                 rf"(?:,?\s*(\d{{4}})\b)?", s)
    if m:
        p.date = (int(m.group(3)) if m.group(3) else None, _MONTH_NAMES[m.group(1)], int(m.group(2)))
    else:
        m, s = _take(rf"\b(?:on\s+)?(?:the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b\.?"
                     rf"(?:,?\s*(\d{{4}})\b)?", s)
        if m:
            p.date = (int(m.group(3)) if m.group(3) else None, _MONTH_NAMES[m.group(2)], int(m.group(1)))
    if p.date is None:
        m, s = _take(r"\b(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)\b", s)
        if m:
            p.date = (None, 0, int(m.group(1)))  # month 0: this month, else next
    m, s = _take(rf"\b(?:(next|this|on|coming|this\s+coming)\s+)?({_DAY_RE})\b\.?", s)
    if m:
        p.weekday = _DAY_NAMES[m.group(2)]
        p.next_week = (m.group(1) or "") == "next"
    return s


def _time(s: str, p: _Parts) -> str:
    m, s2 = _take(r"\b(?:at\s+|by\s+)?(noon|midday|midnight)\b", s)
    if m:
        if m.group(1) == "midnight":
            p.hour, p.minute, p.ampm, p.midnight = 0, 0, "24", True
        else:
            p.hour, p.minute, p.ampm = 12, 0, "24"
        return s2
    m, s2 = _take(r"\b(half|quarter|\d{1,2})\s+(?:minutes?\s+)?(past|after|to|till|til|before)\s+(\d{1,2})\b"
                  r"(?:\s*(am|pm))?", s)
    if m:
        mins = {"half": 30, "quarter": 15}.get(m.group(1))
        mins = int(m.group(1)) if mins is None else mins
        hour = int(m.group(3))
        if mins >= 60 or not 0 <= hour <= 23 or (m.group(2) in ("to", "till", "til", "before") and mins == 30):
            raise WhenError(f"I couldn't read the time '{m.group(0)}'; {HINT}")
        if m.group(2) in ("past", "after"):
            p.hour, p.minute = hour, mins
        else:
            p.hour, p.minute = (hour - 1) % 24, 60 - mins
        p.ampm = m.group(4)
        return s2
    m, s2 = _take(r"\b(?:at\s+|by\s+|for\s+|@\s*)?(\d{1,2})[:.h](\d{2})\b(?:\s*(am|pm))?", s)
    if m:
        return _set_clock(p, m.group(1), m.group(2), m.group(3), s2)
    m, s2 = _take(r"\b(?:at|by|for|around|@)\s*(\d{1,2})\s+([0-5]\d)\b(?:\s*(am|pm))?", s)  # "at 7 30"
    if m:
        return _set_clock(p, m.group(1), m.group(2), m.group(3), s2)
    m, s2 = _take(r"\b(\d{1,2})\s+([0-5]\d)\s*(am|pm)\b", s)  # "7 30 pm" (said "seven thirty pm")
    if m:
        return _set_clock(p, m.group(1), m.group(2), m.group(3), s2)
    m, s2 = _take(r"\b(?:at\s+|by\s+|for\s+|@\s*)?(\d{1,2})\s*(am|pm)\b", s)
    if m:
        return _set_clock(p, m.group(1), "00", m.group(2), s2)
    m, s2 = _take(r"\b(?:at|by|for|around|@)\s*(\d{3,4})\b(?!\s*" + _REL_UNIT + r"\b)", s)  # "at 730", "at 1830"
    if m:
        digits = m.group(1)
        return _set_clock(p, digits[:-2], digits[-2:], None, s2)
    m, s2 = _take(r"\b(?:at|by|for|around|@)\s*(\d{1,2})\b(?!\s*" + _REL_UNIT + r"\b)", s)
    if m:
        return _set_clock(p, m.group(1), "00", None, s2)
    m = re.fullmatch(r"\s*(\d{1,2})\s*", s)  # an alarm for "7"
    if m:
        return _set_clock(p, m.group(1), "00", None, "")
    return s


def _set_clock(p: _Parts, hour: str, minute: str, ampm: str | None, rest: str) -> str:
    h, mi = int(hour), int(minute)
    if mi > 59 or h > 24 or (ampm in ("am", "pm") and not 0 <= h <= 12):
        raise WhenError(f"{hour}:{minute} {ampm or ''} isn't a time of day; {HINT}".replace("  ", " "))
    if h == 24:
        h, p.midnight = 0, True
    p.hour, p.minute = h, mi
    if ampm in ("am", "pm", "24"):
        p.ampm = ampm
    elif h >= 13 or h == 0 or (len(hour) == 2 and hour.startswith("0")):
        p.ampm = "24"
    return rest


def _hours(p: _Parts, *, day_given: bool, alarm: bool) -> list[int]:
    """The 24-hour hour(s) ``p`` may mean, most likely first."""
    h = p.hour if p.hour is not None else 0
    if p.ampm == "24":
        return [h]
    if p.ampm == "am":
        return [0 if h == 12 else h]
    if p.ampm == "pm":
        return [12 if h == 12 else h + 12]
    part = p.daypart
    if part == "morning":
        return [0 if h == 12 else h]
    if part in ("afternoon", "evening"):
        return [h if h == 12 else h + 12]
    if part == "night":
        if h == 12:
            return [0]
        return [h] if h <= 4 else [h + 12]
    if h == 12:
        return [12]
    if day_given:  # "tomorrow at 7", "every weekday at 6", "friday at 3": the usual half of the day
        if alarm:
            return [h]
        return [h] if h >= 7 else [h + 12]
    return [h, h + 12]  # "at 6": whichever comes next


# ---------------------------------------------------------------- the main entry
def parse_repeat(value: object) -> Repeat:
    """A separate ``repeat`` argument: None (once), a weekday set, or "weekly" (the first time's weekday)."""
    if value is None or value is False:
        return None
    if isinstance(value, (list, tuple, set)):
        value = ",".join(str(v) for v in value)
    s = " ".join(str(value).lower().replace("-", " ").replace("_", " ").split()).strip(" .")
    if s in ("", "none", "no", "never", "once", "one time", "false", "0", "null", "off", "only once", "no repeat"):
        return None
    if s in ("daily", "every day", "everyday", "each day", "all days", "day", "days", "every morning", "every night",
             "every evening", "nightly", "true", "yes"):
        return EVERY_DAY
    if s in ("weekdays", "every weekday", "weekday", "work days", "workdays", "mon to fri", "monday to friday",
             "mon fri", "monday friday", "monday through friday", "business days", "on weekdays"):
        return WEEKDAY_SET
    if s in ("weekends", "every weekend", "weekend", "sat and sun", "saturday and sunday", "on weekends"):
        return WEEKEND_SET
    if s in ("weekly", "every week", "week", "once a week", "each week"):
        return "weekly"
    words = re.findall(r"[a-z]+", s.replace("every", " ").replace("each", " "))
    days = set()
    for w in words:
        if w in ("and", "on", "at", "the"):
            continue
        key = w if w in _DAY_NAMES else w.rstrip("s")
        if key not in _DAY_NAMES:
            raise WhenError("repeat can be 'daily', 'weekdays', 'weekends', 'weekly' or days like 'monday,friday' "
                            "(no hourly or monthly repeats)")
        days.add(_DAY_NAMES[key])
    if not days:
        raise WhenError("repeat can be 'daily', 'weekdays', 'weekends', 'weekly' or days like 'monday,friday'")
    return frozenset(days)


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[tT ](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?"
                  r"\s*(z|utc|[+-]\d{2}(?::?\d{2})?)?)?", re.I)


def _iso(raw: str, tz: _dt.tzinfo | None) -> tuple[_dt.datetime | None, _dt.date | None, str]:
    """An ISO-8601 time in ``raw``: (the time, None, the rest of the words), (None, a date alone, the rest) or
    (None, None, raw)."""
    m = _ISO.search(raw)
    if m is None:
        return None, None, raw
    rest = (raw[:m.start()] + " " + raw[m.end():]).strip()
    try:
        day = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise WhenError(f"{m.group(0)} isn't a date") from None
    if m.group(4) is None:
        return None, day, rest
    h, mi, sec = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    if h > 23 or mi > 59 or sec > 59:
        raise WhenError(f"{m.group(0)} isn't a time of day")
    off = (m.group(7) or "").lower()
    if not off:
        return local_dt(day, h, mi, tz, sec), None, rest
    if off in ("z", "utc"):
        offset = _dt.timedelta(0)
    else:
        digits = off[1:].replace(":", "")
        offset = _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        offset = -offset if off[0] == "-" else offset
    at = _dt.datetime(day.year, day.month, day.day, h, mi, sec, tzinfo=_dt.timezone(offset))
    return (at.astimezone(tz) if tz is not None else at.astimezone()), None, rest


def parse_when(text: object, now: _dt.datetime, *, tz: _dt.tzinfo | None = None, alarm: bool = False,
               repeat: Repeat = None) -> When:
    """When ``text`` means, after ``now`` (aware; ``tz`` None: ``now``'s zone, else the robot's). ``repeat``: a
    separate repeat value (:func:`parse_repeat`); one said in ``text`` ("every weekday at 7") wins. ``alarm``: a
    time without am / pm on a given day is the morning, and a day alone is not enough. Raises :class:`WhenError`."""
    raw = " ".join(str(text or "").split())[:120]
    if not raw:
        raise WhenError(f"no time given; {HINT}")
    tz = tz if tz is not None else zone_of(now)
    p = _Parts()
    at, iso_day, rest = _iso(raw, tz)
    s = _repeat_phrase(_clean(rest), p)
    if p.repeat is None:
        p.repeat = repeat
    if at is not None:
        return _finish(at, p.repeat, now, tz)
    if iso_day is not None:
        p.date = (iso_day.year, iso_day.month, iso_day.day)
    s = _relative(s, p)
    s = _day(s, p)
    s = _time(s, p)
    if p.rel is not None and p.rel <= 0:
        raise WhenError("that's no time from now; give a time in the future")
    if p.rel is not None and not (p.rel_day_unit and p.hour is not None):
        if p.repeat:
            raise WhenError("a repeating reminder needs a time of day ('every weekday at 7')")
        if p.rel > MAX_AHEAD.total_seconds():
            raise WhenError("that's more than a year away")
        return When(now + _dt.timedelta(seconds=round(p.rel)))
    named_day = p.day_offset is not None or p.weekday is not None or p.date is not None or p.rel is not None
    if p.hour is None:
        if not named_day and not p.repeat and p.daypart is None:
            raise WhenError(f"I couldn't understand the time '{raw[:60]}'; {HINT}")
        if alarm:
            raise WhenError("an alarm needs a time of day ('7 am', '6:30')")
        hour, p.minute = DAYPART_DEFAULT.get(p.daypart or "", DAY_DEFAULT)
        hours = [hour]
    else:
        hours = _hours(p, day_given=named_day or bool(p.repeat), alarm=alarm)
    # Midnight, and "1 am tonight", are the end of the day they are said with.
    late = p.midnight or (p.daypart == "night" and p.hour is not None and hours[0] < 5)
    if p.repeat:
        start = now
        if named_day:
            start = max(now, local_dt(_first_day(p, now), 0, 0, tz) - _dt.timedelta(seconds=1))
        if p.repeat == "weekly":
            days = frozenset({_first_day(p, now).weekday()}) if p.weekday is not None or p.date is not None \
                else frozenset()
            at = min(next_time(start, h, p.minute, days, tz) for h in hours)
            return _finish(at, frozenset({at.weekday()}), now, tz)
        return _finish(min(next_time(start, h, p.minute, p.repeat, tz) for h in hours), p.repeat, now, tz)
    if not named_day:  # no day: the next time that clock time comes round (today, else tomorrow)
        return _finish(min(next_time(now, h, p.minute, frozenset(), tz) for h in hours), None, now, tz)
    if p.rel is not None:  # "in 2 days at 5 pm"
        day = (now + _dt.timedelta(seconds=p.rel)).astimezone(tz).date() if tz is not None else \
            (now + _dt.timedelta(seconds=p.rel)).date()
    else:
        day = _first_day(p, now, hours[0], p.minute, tz, late)
    if late:
        day += _dt.timedelta(days=1)
    return _finish(local_dt(day, hours[0], p.minute, tz), None, now, tz)


def _first_day(p: _Parts, now: _dt.datetime, hour: int | None = None, minute: int = 0,
               tz: _dt.tzinfo | None = None, late: bool = False) -> _dt.date:
    """The day ``p`` names (a date, today / tomorrow / next week, a weekday). A weekday that is today counts only
    while ``hour:minute`` is still to come (``late``: on the night after it)."""
    today = now.astimezone(tz).date() if tz is not None else now.date()
    if p.date is not None:
        year, month, mday = p.date
        if month == 0:  # "the 15th": this month, else next
            month, year = today.month, today.year
            for _ in range(2):
                try:
                    cand: _dt.date | None = _dt.date(year, month, mday)
                except ValueError:
                    cand = None
                if cand is not None and cand >= today:
                    return cand
                month, year = (1, year + 1) if month == 12 else (month + 1, year)
            raise WhenError(f"there's no day {mday} coming up")
        try:
            cand = _dt.date(year or today.year, month, mday)
        except ValueError:
            raise WhenError(f"{mday} {MONTHS[month - 1].capitalize()} isn't a date") from None
        if year is None and cand < today:
            cand = cand.replace(year=today.year + 1)
        return cand
    if p.weekday is not None:
        if p.day_offset == 7:  # "next week on friday": that day in the week after this one
            return today + _dt.timedelta(days=7 - today.weekday() + p.weekday)
        ahead = (p.weekday - today.weekday()) % 7
        if ahead == 0 and p.next_week:
            ahead = 7
        if ahead == 0 and hour is not None:
            day = today + _dt.timedelta(days=1 if late else 0)
            if local_dt(day, hour, minute, tz) <= now:
                ahead = 7
        return today + _dt.timedelta(days=ahead)
    return today + _dt.timedelta(days=p.day_offset or 0)


def _finish(at: _dt.datetime, repeat: Repeat, now: _dt.datetime, tz: _dt.tzinfo | None) -> When:
    if repeat == "weekly":
        repeat = frozenset({at.weekday()})
    days = frozenset(repeat or ())
    if days and at.weekday() not in days:
        at = next_time(at, at.hour, at.minute, days, tz)
    if at <= now:
        raise WhenError(f"that time has passed (it was {clock_words(at)} {day_words(at, now)}; it's "
                        f"{clock_words(now)} now); give a time in the future")
    if at - now > MAX_AHEAD:
        raise WhenError("that's more than a year away")
    return When(at, days)


__all__ = ["EVERY_DAY", "HINT", "WEEKDAYS", "WEEKDAY_SET", "WEEKEND_SET", "When", "WhenError", "clock_words",
           "day_words", "days_from_text", "days_text", "local_dt", "next_time", "now_local", "parse_repeat",
           "parse_when", "repeat_words", "say_when", "until_words", "zone_of"]
