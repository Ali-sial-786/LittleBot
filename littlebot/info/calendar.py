"""The family's calendars for LittleBot's tools and the dashboard: Google Calendar (or any iCal feed).

Each grown-up can have several calendars ("Ali: Work", "Ali: Personal", "Sam: Personal"). A parent adds
each one on the dashboard (Settings, "Calendars") by pasting the calendar's **secret address in iCal
format** from Google Calendar's settings (docs/CALENDAR.md has the steps). No Google sign-in, no Google
Cloud project, no API key: LittleBot reads the calendar the way any calendar app subscribes to it.

    GET https://calendar.google.com/calendar/ical/<calendar id>/private-<secret>/basic.ics

Read-only: LittleBot never changes a calendar. What it answers ("what's on today?", "is Ali busy on
Friday?", "when is the dentist?") comes from ``agenda()``, used by the ``get_calendar`` tool
(littlebot/chatgpt/tools.py): the events' titles, times and places, never their descriptions or guests.

Kept on the robot only, in its database (table ``calendars``): each calendar's person, its name and its
link. The link is a password (anyone with it can read the calendar): it is never logged, never sent to the
dashboard's pages (they see "calendar.google.com, secret address") and never sent to the AI. Removing a
calendar deletes its link. To make an old link useless, reset the secret address in Google Calendar.

Fast on purpose (the live voice waits a few seconds for an answer): a calendar is fetched at most once
every ``calendar.cache_minutes`` (10); an older copy (up to ``calendar.max_stale_hours``) answers at once
while a fresh one is fetched in the background; every answer takes at most ``calendar.timeout_seconds``,
and a calendar that is slower is left out of that answer (it carries on, so the next question has it).
Nothing is fetched in offline mode or while cloud communication is off in Privacy.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import calendar as _monthcal
import datetime as _dt
import logging
import re
import time
import unicodedata
import urllib.parse
import zlib
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger("littlebot.info.calendar")

MAX_CALENDARS = 20
MAX_DAYS = 31  # the longest stretch one question looks at
SEARCH_DAYS = 31  # "when is the dentist?": how far ahead a search looks when no days are given
SOON_DAYS = 14  # the dashboard's "events in the next two weeks"
MAX_EVENTS = 40  # per answer (the rest are counted)
MAX_BYTES = 15 * 1024 * 1024  # a calendar feed bigger than this is refused (years of busy calendar are ~5 MB)
MAX_LINK = 2000
NAME_MAX = 40
TITLE_MAX = 120
PLACE_MAX = 100

ERRORS = {
    "offline": "LittleBot is in offline mode, so it can't read the calendars",
    "cloud_off": "cloud communication is off in Privacy, so LittleBot can't read the calendars",
    "not_installed": "the calendar reader isn't installed on this robot yet (run scripts/install_pi.sh again, "
                     "or .venv/bin/python -m pip install -e .)",
    "no_calendars": "no calendars are set up yet; a grown-up can add them on the dashboard (Settings, Calendars)",
    "whose": "say whose calendar, by name: the calendars are {what}",
    "unknown_person": "there is no calendar for {what}",
    "unknown_calendar": "there is no calendar called {what}",
    "bad_date": "LittleBot couldn't tell which day {what} is; say a day like today, tomorrow, Friday, or a date",
    "bad_link": "that isn't a calendar link",
    "not_found": "the calendar wasn't found at that link (it may have been reset, or isn't shared)",
    "not_calendar": "that link opens a web page, not a calendar",
    "too_big": "that calendar is too big for LittleBot to read",
    "timeout": "the calendar didn't answer in time",
    "network": "the calendar couldn't be reached (is the robot online?)",
    "limited": "the calendar service is busy (too many requests) and asked LittleBot to try again later",
    "bad_response": "the calendar gave an answer LittleBot couldn't read",
}
# For the parent on the dashboard: what to do about it.
HELP = {
    "bad_link": "Paste the calendar's “Secret address in iCal format” from Google Calendar's settings. It starts "
                "with https://calendar.google.com/calendar/ical/ and ends with .ics.",
    "not_found": "Google says there is no calendar at that link. Copy the “Secret address in iCal format” again "
                 "(it changes if it was reset). The “Public address” only works for calendars made public.",
    "not_calendar": "That link opens a web page. Copy the address that ends in .ics: the “Secret address in iCal "
                    "format”, under Integrate calendar in the calendar's settings.",
    "too_big": "That calendar is over 15 MB. Try a calendar with fewer events.",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL,
    label TEXT NOT NULL,
    url TEXT NOT NULL,               -- the secret link: a password, never logged or shown
    name TEXT NOT NULL DEFAULT '',   -- the calendar's own name (X-WR-CALNAME), from the last read
    added_at TEXT NOT NULL,
    checked_at TEXT,                 -- the last read (UTC), successful or not
    problem TEXT,                    -- why the last read failed (a key of ERRORS), NULL = it worked
    events_soon INTEGER              -- events in the next SOON_DAYS days at the last read
);
"""

_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3,
             "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_MONTHS = {name.lower(): i for i, name in enumerate(_monthcal.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(_monthcal.month_abbr) if name})
_MONTHS["sept"] = 9
# "Everyone's": no one person. "My" / "me": the person talking, whom only the model may know.
_EVERYONE = {"everyone", "everybody", "all", "family", "the family", "our", "ours", "us", "we", "all of us"}
_ME = {"me", "my", "mine", "i", "myself"}
_SEARCH_STOP = {"the", "and", "for", "with", "when", "what", "next", "is", "my", "our", "appointment", "appointments",
                "meeting", "meetings", "event", "events", "calendar", "calendars", "thing", "things", "on", "at",
                "a", "an", "of", "to", "in"}


class CalendarError(Exception):
    """Why there is no calendar answer: ``kind`` is a key of ERRORS; str() is a plain sentence."""

    def __init__(self, kind: str, what: str | None = None) -> None:
        self.kind = kind if kind in ERRORS else "bad_response"
        self.what = what
        super().__init__(ERRORS[self.kind].format(what=(what or "that")[:400]))

    @property
    def help(self) -> str:
        """What the parent can do about it (the dashboard), or the plain sentence."""
        return HELP.get(self.kind) or (str(self)[:1].upper() + str(self)[1:] + ".")


def installed() -> bool:
    try:
        import icalendar  # noqa: F401, PLC0415
        import recurring_ical_events  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------- links
def normalize_link(text: Any) -> str:
    """The calendar feed's https address from what a parent pasted. Raises CalendarError("bad_link").

    Accepts the secret or public iCal address (https:// or webcal://), and for public Google calendars also
    the calendar ID ("abc123@group.calendar.google.com"), an embed link (".../embed?src=...") or a
    subscribe link (".../calendar/u/0?cid=..."), which become the calendar's public iCal address."""
    raw = "".join(ch for ch in str(text or "") if not ch.isspace() and unicodedata.category(ch)[0] != "C")
    if not raw or len(raw) > MAX_LINK:
        raise CalendarError("bad_link")
    if "://" not in raw and "@" in raw and "/" not in raw:  # a calendar ID
        return _google_public(raw)
    if raw.lower().startswith(("webcal://", "webcals://")):
        raw = "https://" + raw.split("://", 1)[1]
    elif "://" not in raw and re.match(r"^[\w-]+(\.[\w-]+)+/", raw):  # "calendar.google.com/calendar/ical/..."
        raw = "https://" + raw
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError):
        raise CalendarError("bad_link") from None
    if url.scheme != "https" or not url.host or url.userinfo:
        raise CalendarError("bad_link")
    if url.host in ("calendar.google.com", "www.google.com") and "/ical/" not in url.path:
        params = urllib.parse.parse_qs(url.query.decode("ascii", "replace"))
        src = (params.get("src") or [""])[0]
        cid = (params.get("cid") or [""])[0]
        found = src or _decode_cid(cid)
        if found.lower().startswith(("https://", "webcal://")):
            return normalize_link(found)
        if "@" in found:
            return _google_public(found)
        raise CalendarError("bad_link")
    return str(url)


def _google_public(calendar_id: str) -> str:
    calendar_id = urllib.parse.unquote(calendar_id).strip()
    if not re.fullmatch(r"[\w.+%-]+@[\w.-]+\.[a-z]{2,}", calendar_id, re.I):
        raise CalendarError("bad_link")
    return f"https://calendar.google.com/calendar/ical/{urllib.parse.quote(calendar_id, safe='')}/public/basic.ics"


def _decode_cid(cid: str) -> str:
    """Google's subscribe links carry the calendar ID in base64 (or as it is)."""
    if not cid or "@" in cid or "://" in cid:
        return cid
    try:
        return base64.urlsafe_b64decode(cid + "=" * (-len(cid) % 4)).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return ""


def link_kind(url: str) -> str:
    """ "google_secret" | "google_public" | "other" """
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return "other"
    if u.host == "calendar.google.com" and "/ical/" in u.path:
        return "google_secret" if "/private-" in u.path else "google_public"
    return "other"


def masked(url: str) -> str:
    """What the dashboard shows of a link: "calendar.google.com, secret address". Never the link itself."""
    kind = link_kind(url)
    try:
        host = httpx.URL(url).host or "?"
    except (httpx.InvalidURL, ValueError):
        host = "?"
    return {"google_secret": f"{host}, secret address", "google_public": f"{host}, public address"}.get(
        kind, f"{host}, calendar link")


# ---------------------------------------------------------------- names
def clean_name(value: Any, *, field: str = "name") -> str:
    """A person's or calendar's name: letters, digits, spaces and . ' - & ( ), at most NAME_MAX characters."""
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split())
    if not text:
        raise ValueError(f"Type a {field}.")
    if len(text) > NAME_MAX:
        raise ValueError(f"The {field} can be at most {NAME_MAX} characters.")
    if not re.fullmatch(r"[\w .'’&()\-]+", text) or not any(ch.isalpha() for ch in text):
        raise ValueError(f"The {field} can only have letters, numbers, spaces and . ' - &.")
    return text


def _norm(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower().replace("’", "'"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.replace("'s ", " ").replace("'", "")).strip()


def _who(person: Any) -> str:
    """ "Ali's calendar" -> "ali" """
    words = _norm(f"{person} ").split()
    while words and words[-1] in ("calendar", "calendars", "diary", "schedule", "s"):
        words.pop()
    return " ".join(words)


def _which(label: Any) -> str:
    words = [w for w in _norm(label).split() if w not in ("calendar", "calendars", "the", "my", "diary")]
    return " ".join(words)


# ---------------------------------------------------------------- days
def parse_day(text: Any, today: _dt.date) -> tuple[_dt.date, int | None]:
    """The first day asked about, and how many days the words mean (None: they don't say).

    "today", "tomorrow", "friday" / "next friday", "this week" (today to Sunday), "next week", "this weekend",
    "this month", "next month", "2026-11-20", "20 November", "November 20th", "the 20th". CalendarError
    ("bad_date") otherwise."""
    t = " ".join(re.sub(r"[^a-z0-9\- ]+", " ", str(text or "").lower()).split())
    t = re.sub(r"^(?:on|for|from|this coming|coming|the day of)\s+", "", t)
    if t in ("", "today", "now", "tonight", "this morning", "this afternoon", "this evening", "today s", "todays"):
        return today, None
    if t.startswith("today"):
        return today, None
    if t in ("day after tomorrow", "the day after tomorrow", "overmorrow"):
        return today + _dt.timedelta(days=2), None
    if t.startswith("tomorrow"):
        return today + _dt.timedelta(days=1), None
    if t.startswith("yesterday"):
        return today - _dt.timedelta(days=1), None
    if t in ("this week", "week", "the week", "the rest of the week", "rest of the week", "the week ahead"):
        return today, 7 - today.weekday()
    if t in ("next week", "the next week"):
        return today + _dt.timedelta(days=7 - today.weekday()), 7
    if t in ("next 7 days", "the next 7 days", "the next seven days", "next seven days", "seven days", "7 days"):
        return today, 7
    if t in ("this weekend", "weekend", "the weekend"):
        if today.weekday() == 6:  # Sunday: what is left of it
            return today, 1
        return today + _dt.timedelta(days=5 - today.weekday()), 2
    if t == "next weekend":
        return today + _dt.timedelta(days=7 - today.weekday() + 5), 2
    if t in ("this month", "month", "the rest of the month"):
        last = _monthcal.monthrange(today.year, today.month)[1]
        return today, min(MAX_DAYS, last - today.day + 1)
    if t == "next month":
        first = (today.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        return first, min(MAX_DAYS, _monthcal.monthrange(first.year, first.month)[1])
    m = re.fullmatch(r"(this |next )?([a-z]+)", t)
    if m and m.group(2) in _WEEKDAYS:
        ahead = (_WEEKDAYS[m.group(2)] - today.weekday()) % 7
        if m.group(1) == "next " and ahead == 0:
            ahead = 7
        return today + _dt.timedelta(days=ahead), None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)), text), None
    m = (re.fullmatch(r"(?:the )?(\d{1,2})(?:st|nd|rd|th)?(?: of)? ([a-z]+)(?: (\d{4}))?", t)
         or re.fullmatch(r"([a-z]+) (?:the )?(\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", t))
    if m:
        a, b, year = m.group(1), m.group(2), m.group(3)
        day, month = (a, b) if a.isdigit() else (b, a)
        m = m if month in _MONTHS else None  # "the 20th": not a month
    if m:
        found = _date(int(year) if year else today.year, _MONTHS[month], int(day), text)
        if not year and found < today - _dt.timedelta(days=7):
            found = _date(today.year + 1, _MONTHS[month], int(day), text)
        return found, None
    m = re.fullmatch(r"(?:the )?(\d{1,2})(?:st|nd|rd|th)", t)
    if m:
        day = int(m.group(1))
        found = None
        for months_ahead in (0, 1, 2):  # this month, else the next month that has that day
            first = (today.replace(day=1) + _dt.timedelta(days=32 * months_ahead)).replace(day=1)
            if day <= _monthcal.monthrange(first.year, first.month)[1]:
                candidate = first.replace(day=day)
                if candidate >= today:
                    found = candidate
                    break
        if found is None:
            raise CalendarError("bad_date", str(text))
        return found, None
    raise CalendarError("bad_date", str(text))


def _date(year: int, month: int, day: int, text: Any) -> _dt.date:
    try:
        return _dt.date(year, month, day)
    except ValueError:
        raise CalendarError("bad_date", str(text)) from None


def day_label(day: _dt.date, today: _dt.date) -> str:
    if day == today:
        return "today"
    if day == today + _dt.timedelta(days=1):
        return "tomorrow"
    if day == today - _dt.timedelta(days=1):
        return "yesterday"
    return f"{day:%A}"


def date_text(day: _dt.date) -> str:
    """ "Thursday 5 November" """
    return f"{day:%A} {day.day} {day:%B}"


def when_text(day: _dt.date, today: _dt.date) -> str:
    """ "today, Tuesday 3 November", "Thursday 5 November" """
    label = day_label(day, today)
    return f"{label}, {date_text(day)}" if label in ("today", "tomorrow", "yesterday") else date_text(day)


# ---------------------------------------------------------------- reading a feed
WINDOW_BACK_DAYS = 1  # each read works out the events from yesterday...
WINDOW_DAYS = 92  # ...to about three months ahead; a question outside that reads the kept copy again
_DATE_LINE = re.compile(rb"^(DTSTART|DTEND)[^:]*:(\d{8})", re.I)


@dataclass(frozen=True)
class Event:
    title: str
    start: _dt.datetime | _dt.date  # an aware datetime in the robot's zone, or a date (all day)
    end: _dt.datetime | _dt.date  # (all day: the day after the last one, as in iCal)
    all_day: bool
    location: str = ""


@dataclass
class _Feed:
    """One calendar as last read: its events over a window of days (small), and the file itself, compressed,
    for a question outside that window. The parsed calendar isn't kept (a busy one is 50+ MB in memory)."""

    name: str
    events: list[Event]
    first: _dt.date  # the window: first..last day (inclusive)
    last: _dt.date
    raw: bytes  # zlib
    fetched_at: float  # monotonic


def trim_feed(data: bytes, keep_from: _dt.date) -> bytes:
    """``data`` without the one-off events that ended before ``keep_from`` (a calendar keeps every event since it
    was made: reading only the rest is 10-100 times faster). Repeating events and their changes are all kept."""
    data = data.replace(b"\r\n ", b"").replace(b"\r\n\t", b"").replace(b"\n ", b"").replace(b"\n\t", b"")
    cutoff = int(keep_from.strftime("%Y%m%d"))
    out: list[bytes] = []
    block: list[bytes] | None = None
    start = end = None
    repeats = False
    for line in data.splitlines():
        upper = line[:16].upper()
        if block is None:
            if upper.startswith(b"BEGIN:VEVENT"):
                block, start, end, repeats = [line], None, None, False
            else:
                out.append(line)
            continue
        block.append(line)
        if upper.startswith((b"RRULE", b"RDATE", b"RECURRENCE-ID", b"EXRULE")):
            repeats = True
        else:
            m = _DATE_LINE.match(line)
            if m:
                if m.group(1).upper() == b"DTSTART":
                    start = int(m.group(2))
                else:
                    end = int(m.group(2))
        if upper.startswith(b"END:VEVENT"):
            last = end if end is not None else start
            if repeats or last is None or last >= cutoff:
                out.extend(block)
            block = None
    if block:
        out.extend(block)
    return b"\r\n".join(out) + b"\r\n"


def parse_feed(data: bytes) -> tuple[Any, str]:
    """(recurring-event query, the calendar's own name) from an .ics file. Raises CalendarError."""
    try:
        import icalendar  # noqa: PLC0415
        import recurring_ical_events  # noqa: PLC0415
    except ImportError:
        raise CalendarError("not_installed") from None
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception:  # noqa: BLE001 - the library raises many kinds on a broken file
        raise CalendarError("not_calendar") from None
    if isinstance(cal, list):  # several calendars in one file: the first
        cal = cal[0] if cal else None
    if cal is None or getattr(cal, "name", "") != "VCALENDAR":
        raise CalendarError("not_calendar")
    name = " ".join(str(cal.get("X-WR-CALNAME") or "").split())[:80]
    try:
        return recurring_ical_events.of(cal, skip_bad_series=True), name
    except Exception:  # noqa: BLE001
        raise CalendarError("bad_response") from None


def read_events(data: bytes, first: _dt.date, last: _dt.date, tz: _dt.tzinfo) -> tuple[str, list[Event]]:
    """(the calendar's name, its events from ``first`` to ``last`` inclusive, sorted). Slow: run it in a thread."""
    query, name = parse_feed(trim_feed(data, first - _dt.timedelta(days=2)))
    start = _dt.datetime.combine(first, _dt.time(0), tz)
    end = _dt.datetime.combine(last + _dt.timedelta(days=1), _dt.time(0), tz)
    try:
        events = events_between(query, start, end, tz)
    except CalendarError:
        raise
    except Exception:  # noqa: BLE001 - a broken series the library didn't skip
        log.warning("calendar: working out a calendar's events failed", exc_info=True)
        raise CalendarError("bad_response") from None
    events.sort(key=lambda ev: _sort_key(ev, tz))
    return name, events


def events_between(query: Any, start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo) -> list[Event]:
    """The events from ``start`` to ``end`` (aware datetimes), times in ``tz``; cancelled ones left out."""
    out: list[Event] = []
    for comp in query.between(start, end):
        if str(comp.get("STATUS") or "").upper() == "CANCELLED":
            continue
        try:
            s = comp.decoded("DTSTART")
        except (KeyError, ValueError):
            continue
        e = None
        try:
            if "DTEND" in comp:
                e = comp.decoded("DTEND")
            elif "DURATION" in comp:
                e = s + comp.decoded("DURATION")
        except (KeyError, ValueError, TypeError):
            e = None
        if isinstance(s, _dt.datetime):
            s = _local(s, tz)
            e = _local(e, tz) if isinstance(e, _dt.datetime) and _local(e, tz) >= s else s
            all_day = False
        elif isinstance(s, _dt.date):
            e = e if isinstance(e, _dt.date) and not isinstance(e, _dt.datetime) and e > s else s + _dt.timedelta(1)
            all_day = True
        else:
            continue
        title = " ".join(str(comp.get("SUMMARY") or "").split())[:TITLE_MAX] or "Busy"
        place = " ".join(str(comp.get("LOCATION") or "").split())
        if place.lower().startswith(("http://", "https://")):
            place = ""  # a video-call link: not worth saying
        out.append(Event(title, s, e, all_day, place[:PLACE_MAX]))
    return out


def _local(value: _dt.datetime, tz: _dt.tzinfo) -> _dt.datetime:
    """An aware datetime in ``tz`` (a "floating" time without a zone is already local)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _span(ev: Event, tz: _dt.tzinfo) -> tuple[_dt.datetime, _dt.datetime]:
    if ev.all_day:
        return (_dt.datetime.combine(ev.start, _dt.time(0), tz),  # type: ignore[arg-type]
                _dt.datetime.combine(ev.end, _dt.time(0), tz))  # type: ignore[arg-type]
    return ev.start, ev.end  # type: ignore[return-value]


def overlapping(events: list[Event], start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo) -> list[Event]:
    """The events that are on at some point from ``start`` to ``end`` (one with no length: that starts then)."""
    out = []
    for ev in events:
        s, e = _span(ev, tz)
        if (s < end and e > start) or (s == e and start <= s < end):
            out.append(ev)
    return out


# ---------------------------------------------------------------- the store
def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class CalendarStore:
    """The calendars a parent added, in ``ctx.db`` (thread-safe: the database locks)."""

    def __init__(self, db: Any) -> None:
        self.db = db
        db.executescript(_SCHEMA)

    def all(self) -> list[dict[str, Any]]:
        """Every calendar, with its link (for the service only: never hand these rows to the dashboard)."""
        return self.db.query("SELECT * FROM calendars ORDER BY person COLLATE NOCASE, id")

    def get(self, cal_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM calendars WHERE id = ?", (int(cal_id),))

    def add(self, person: Any, label: Any, url: str) -> dict[str, Any]:
        person, label = clean_name(person, field="person's name"), clean_name(label, field="calendar name")
        self.check_new(person, label, url)
        cur = self.db.execute("INSERT INTO calendars (person, label, url, added_at) VALUES (?, ?, ?, ?)",
                              (person, label, url, _utc_now()))
        return self.get(int(cur.lastrowid)) or {}

    def update(self, cal_id: int, *, person: Any = None, label: Any = None, url: str | None = None) -> dict[str, Any]:
        row = self.get(cal_id)
        if row is None:
            raise KeyError("That calendar isn't on the list.")
        person = clean_name(person, field="person's name") if person is not None else row["person"]
        label = clean_name(label, field="calendar name") if label is not None else row["label"]
        url = url if url is not None else row["url"]
        self.check_new(person, label, url, skip=int(row["id"]))
        self.db.execute("UPDATE calendars SET person = ?, label = ?, url = ? WHERE id = ?",
                        (person, label, url, int(cal_id)))
        if url != row["url"]:
            self.db.execute("UPDATE calendars SET name = '', checked_at = NULL, problem = NULL, events_soon = NULL "
                            "WHERE id = ?", (int(cal_id),))
        return self.get(cal_id) or {}

    def remove(self, cal_id: int) -> bool:
        cur = self.db.execute("DELETE FROM calendars WHERE id = ?", (int(cal_id),))
        return cur.rowcount > 0

    def mark(self, cal_id: int, *, problem: str | None, name: str | None = None, soon: int | None = None) -> None:
        """What the last read found (the dashboard shows it)."""
        if problem is None:
            self.db.execute("UPDATE calendars SET checked_at = ?, problem = NULL, name = ?, events_soon = ? "
                            "WHERE id = ?", (_utc_now(), name or "", soon, int(cal_id)))
        else:
            self.db.execute("UPDATE calendars SET checked_at = ?, problem = ? WHERE id = ?",
                            (_utc_now(), problem, int(cal_id)))

    def check_new(self, person: Any, label: Any, url: str, *, skip: int | None = None) -> None:
        """ValueError when this calendar can't be added (or changed to this): the list is full, the link is on it
        already, or the person has a calendar of that name. ``label`` None: not known yet (not compared)."""
        rows = [r for r in self.all() if r["id"] != skip]
        if skip is None and len(rows) >= MAX_CALENDARS:
            raise ValueError(f"LittleBot can read at most {MAX_CALENDARS} calendars. Remove one first.")
        for r in rows:
            if r["url"] == url:
                raise ValueError(f"That calendar is already on the list ({r['person']}, {r['label']}).")
            if label is not None and _who(r["person"]) == _who(person) and _which(r["label"]) == _which(label):
                raise ValueError(f"{r['person']} already has a calendar called {r['label']}. Give this one "
                                 "another name.")


# ---------------------------------------------------------------- the service
LINK_PROBLEMS = ("bad_link", "not_found", "not_calendar", "too_big")  # the link is wrong: not kept


class CalendarService:
    """``get_calendars(ctx)``: one per robot, shared by the tools and the dashboard."""

    def __init__(self, ctx: Any, *, transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], float] = time.monotonic,
                 clock: Callable[[], _dt.datetime] | None = None) -> None:
        self.ctx = ctx
        self.store = CalendarStore(ctx.db)
        self._transport = transport
        self._now = now
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._feeds: dict[int, tuple[str, _Feed]] = {}  # id -> (the link it was read from, what it read)
        self._pending: dict[int, asyncio.Task[_Feed]] = {}
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None

    @property
    def cfg(self) -> Any:
        return self.ctx.config.calendar

    # ------------------------------------------------------------ gates and basics
    def blocked_reason(self) -> str | None:
        if self.ctx.config.is_offline:
            return "offline"
        if not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        return None

    def has_calendars(self) -> bool:
        return self.ctx.db.query_one("SELECT 1 AS x FROM calendars LIMIT 1") is not None

    def people(self) -> list[str]:
        return list(dict.fromkeys(r["person"] for r in self.store.all()))

    def tz(self) -> _dt.tzinfo:
        """The robot's time zone: ``info.timezone``, else the system's (by name, so summer time is right)."""
        from littlebot.info.clock import local_zone_name, zone  # noqa: PLC0415

        for name in (getattr(self.ctx.config.info, "timezone", "") or "", local_zone_name() or ""):
            try:
                tz = zone(name)
            except ValueError:
                tz = None
            if tz is not None:
                return tz
        return self._clock().astimezone().tzinfo or _dt.timezone.utc

    def _today(self, tz: _dt.tzinfo) -> tuple[_dt.datetime, _dt.date]:
        now = self._clock().astimezone(tz)
        return now, now.date()

    # ------------------------------------------------------------ for the dashboard
    def status(self) -> dict[str, Any]:
        """What the dashboard shows (nothing is fetched). Never a link, only ``masked()``."""
        reason = self.blocked_reason()
        rows = self.store.all()
        tz = self.tz()
        return {
            "calendars": [self._public(r) for r in rows],
            "people": list(dict.fromkeys(r["person"] for r in rows)),
            "count": len(rows),
            "max": MAX_CALENDARS,
            "installed": installed(),
            "blocked": reason,
            "blocked_detail": ERRORS[reason] if reason else None,
            "timezone": getattr(tz, "key", None) or str(tz),
        }

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        problem = row.get("problem")
        return {"id": int(row["id"]), "person": row["person"], "label": row["label"], "name": row.get("name") or "",
                "link": masked(row["url"]), "link_kind": link_kind(row["url"]), "added_at": row["added_at"],
                "checked_at": row.get("checked_at"), "ok": None if not row.get("checked_at") else problem is None,
                "problem": problem, "problem_detail": CalendarError(problem).help if problem else None,
                "events_soon": row.get("events_soon")}

    def public(self, cal_id: int) -> dict[str, Any] | None:
        row = self.store.get(cal_id)
        return self._public(row) if row else None

    async def add(self, person: Any, label: Any, link: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Add a calendar; the link is read first. Returns (the calendar, what the read found).

        A wrong link (not a calendar, not found) raises CalendarError and nothing is kept. One that can't be
        read right now (offline, cloud off, no answer in time) is kept, and the result says why."""
        url = normalize_link(link)
        label_text = " ".join(str(label or "").split())
        clean_name(person, field="person's name")  # ValueError before anything is fetched
        if label_text:
            clean_name(label_text, field="calendar name")
        self.store.check_new(person, label_text or None, url)
        check, feed = await self._try(url)
        if check["problem"] in LINK_PROBLEMS:
            raise CalendarError(check["problem"])
        row = self.store.add(person, label_text or _default_label(check.get("name") or ""), url)
        cal_id = int(row["id"])
        self._keep(cal_id, url, check, feed)
        log.info("calendar: a calendar was added (%s; %s)", link_kind(url),
                 "read" if check["ok"] else f"not read yet: {check['problem']}")
        return self.public(cal_id) or self._public(row), check

    async def update(self, cal_id: int, *, person: Any = None, label: Any = None,
                     link: Any = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Rename a calendar or its person, or give it a new link (read first, like ``add``)."""
        row = self.store.get(cal_id)
        if row is None:
            raise KeyError("That calendar isn't on the list.")
        url = normalize_link(link) if link not in (None, "") else row["url"]
        check = feed = None
        if url != row["url"]:
            self.store.check_new(person if person is not None else row["person"],
                                 label if label is not None else row["label"], url, skip=int(cal_id))
            check, feed = await self._try(url)
            if check["problem"] in LINK_PROBLEMS:
                raise CalendarError(check["problem"])
        self.store.update(cal_id, person=person, label=label, url=url)
        if check is not None:
            self._feeds.pop(int(cal_id), None)
            self._keep(int(cal_id), url, check, feed)
        log.info("calendar: a calendar was changed%s", " (new link)" if check is not None else "")
        return self.public(cal_id) or {}, check

    def remove(self, cal_id: int) -> bool:
        self._feeds.pop(int(cal_id), None)
        gone = self.store.remove(cal_id)
        if gone:
            log.info("calendar: a calendar was removed")
        return gone

    async def check(self, cal_id: int) -> dict[str, Any]:
        """Read one calendar now (the dashboard's "Check" button): ok or why not, its name, events soon, the next."""
        row = self.store.get(cal_id)
        if row is None:
            raise KeyError("That calendar isn't on the list.")
        check, feed = await self._try(row["url"])
        self._keep(int(row["id"]), row["url"], check, feed)
        return {**check, "calendar": self.public(cal_id)}

    async def _try(self, url: str) -> tuple[dict[str, Any], _Feed | None]:
        """Read ``url`` now (a parent waits: up to 10 s): ({"ok", "problem", "detail", "name", "events_soon",
        "next"}, what it read)."""
        reason = self.blocked_reason()
        if reason:
            return {"ok": False, "problem": reason, "detail": CalendarError(reason).help}, None
        try:
            feed = await asyncio.wait_for(self._read(url), max(10.0, float(self.cfg.timeout_seconds)))
        except asyncio.TimeoutError:
            return {"ok": False, "problem": "timeout", "detail": CalendarError("timeout").help}, None
        except CalendarError as e:
            return {"ok": False, "problem": e.kind, "detail": e.help}, None
        soon, upcoming = self._soon(feed)
        return {"ok": True, "problem": None, "name": feed.name, "events_soon": soon, "next": upcoming}, feed

    def _keep(self, cal_id: int, url: str, check: dict[str, Any], feed: _Feed | None) -> None:
        if feed is not None:
            self._feeds[cal_id] = (url, feed)
            self.store.mark(cal_id, problem=None, name=feed.name, soon=check.get("events_soon"))
        elif check.get("problem") not in (None, "offline", "cloud_off"):
            self.store.mark(cal_id, problem=check["problem"])

    def _soon(self, feed: _Feed) -> tuple[int, str | None]:
        """How many events in the next SOON_DAYS days, and the next one ("Dentist: Thursday 5 November 12:00")."""
        tz = self.tz()
        now, today = self._today(tz)
        events = overlapping(feed.events, now, now + _dt.timedelta(days=SOON_DAYS), tz)
        upcoming = next((ev for ev in events if _span(ev, tz)[0] >= now or ev.all_day), None)
        if upcoming is None:
            return len(events), None
        day = _start_date(upcoming, tz)
        when = day_label(day, today) if day_label(day, today) in ("today", "tomorrow") else date_text(day)
        return len(events), f"{upcoming.title}: {when}" + ("" if upcoming.all_day else f" {upcoming.start:%H:%M}")

    # ------------------------------------------------------------ for the tools
    async def agenda(self, *, person: Any = None, calendar: Any = None, day: Any = None, days: Any = None,
                     search: Any = None) -> dict[str, Any]:
        """What is on: the events from ``day`` (default today) for ``days`` days (default 1, or what the day's words
        mean: "this week"), for ``person`` (default everyone) and ``calendar`` ("work"; default all of theirs),
        optionally only those whose title or place has a word of ``search``. Raises CalendarError."""
        reason = self.blocked_reason()
        if reason:
            raise CalendarError(reason)
        if not installed():
            raise CalendarError("not_installed")
        rows = self.store.all()
        if not rows:
            raise CalendarError("no_calendars")
        rows = self._pick(rows, person, calendar)
        tz = self.tz()
        now, today = self._today(tz)
        first, meant = parse_day(day, today)
        words = _search_words(search)
        try:
            n = int(days) if days not in (None, "") else None
        except (TypeError, ValueError):
            n = None
        n = max(1, min(MAX_DAYS, n or meant or (SEARCH_DAYS if words else 1)))
        last = first + _dt.timedelta(days=n - 1)
        start = _dt.datetime.combine(first, _dt.time(0), tz)
        end = _dt.datetime.combine(last + _dt.timedelta(days=1), _dt.time(0), tz)
        if words and first == today and not day:
            start = now.replace(second=0, microsecond=0)  # "when is the dentist?": from now on

        feeds, missing = await self._feeds_for(rows)
        read = [r for r in rows if int(r["id"]) in feeds]
        results = await asyncio.gather(*(self._events(feeds[int(r["id"])], start, end, tz) for r in read),
                                       return_exceptions=True)
        found: list[tuple[Event, dict[str, Any]]] = []
        for row, got in zip(read, results, strict=True):
            if isinstance(got, BaseException):
                if not isinstance(got, CalendarError):
                    log.warning("calendar: reading one calendar's events failed (%s)", type(got).__name__)
                missing.append((row, got.kind if isinstance(got, CalendarError) else "bad_response"))
                continue
            found.extend((ev, row) for ev in got)
        if words:
            found = [(ev, row) for ev, row in found if _matches(ev, words)]
        listed = _merge(found, tz)
        listed.sort(key=lambda item: _sort_key(item[0], tz))
        events = [_event_out(ev, people, labels, first, today, tz) for ev, people, labels in listed[:MAX_EVENTS]]
        result: dict[str, Any] = {
            "from": when_text(first, today),
            "to": when_text(last, today) if n > 1 else None,
            "now": f"{now:%A} {now:%H:%M}",
            "calendars_read": [f"{r['person']}'s {r['label']}" for r in rows if int(r["id"]) in feeds
                               and all(m[0] is not r for m in missing)],
            "events": events,
            "more_events": len(listed) - MAX_EVENTS if len(listed) > MAX_EVENTS else None,
            "looked_for": " ".join(words) or None,
            "not_read": [{"calendar": f"{r['person']}'s {r['label']}", "why": ERRORS[why]} for r, why in missing]
            or None,
        }
        if not events:
            if missing and len(missing) == len(rows):
                result["note"] = "no calendar could be read: " + ERRORS[missing[0][1]]
            elif words:
                result["note"] = "nothing found with those words"
            else:
                result["note"] = "nothing on the calendar " + ("those days" if n > 1 else "that day")
        return {k: v for k, v in result.items() if v not in (None, [], "")}

    def _pick(self, rows: list[dict[str, Any]], person: Any, calendar: Any) -> list[dict[str, Any]]:
        """The calendars asked about: everyone's, one person's, one of theirs ("work"). Raises CalendarError."""
        who = _who(person) if person else ""
        if who in _EVERYONE:
            who = ""
        if who in _ME:
            owners = list(dict.fromkeys(r["person"] for r in rows))
            if len(owners) != 1:
                raise CalendarError("whose", _join(_owners(rows)))
            who = _who(owners[0])
        if who:
            mine = [r for r in rows if _who(r["person"]) == who] or \
                [r for r in rows if who in _who(r["person"]).split() or _who(r["person"]) in who.split()]
            if not mine:
                raise CalendarError("unknown_person", f"{str(person)[:40]}; the calendars are {_join(_owners(rows))}")
            rows = mine
        which = _which(calendar) if calendar else ""
        if which and which not in ("all", "every", "both", "everything", "all of them"):
            picked = [r for r in rows if _which(r["label"]) == which] or \
                [r for r in rows if which in _which(r["label"]) or which in _which(r.get("name") or "")
                 or any(w in _which(r["label"]).split() for w in which.split())]
            if not picked:
                raise CalendarError("unknown_calendar", f"{str(calendar)[:40]}; the calendars are "
                                                        f"{_join(_owners(rows))}")
            rows = picked
        return rows

    async def _events(self, feed: _Feed, start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo) -> list[Event]:
        """The feed's events from ``start`` to ``end``: from its window, else worked out again from the kept file."""
        if feed.first <= start.date() and (end - _dt.timedelta(microseconds=1)).date() <= feed.last:
            return overlapping(feed.events, start, end, tz)
        data = zlib.decompress(feed.raw)
        _, events = await asyncio.to_thread(read_events, data, start.date(),
                                            (end - _dt.timedelta(microseconds=1)).date(), tz)
        return overlapping(events, start, end, tz)

    # ------------------------------------------------------------ reading the calendars
    async def warm(self) -> None:
        """Read every calendar not read lately (a voice chat starting: the first question answers at once)."""
        if self.blocked_reason() or not installed():
            return
        rows = self.store.all()
        if rows:
            await self._feeds_for(rows)

    async def _feeds_for(self, rows: list[dict[str, Any]]) -> tuple[dict[int, _Feed], list[tuple[dict[str, Any], str]]]:
        """The calendars' feeds: a recent copy at once (an older one too, while a fresh one is read for next time),
        else read now, waiting at most ``calendar.timeout_seconds`` (a slower read carries on for next time)."""
        feeds: dict[int, _Feed] = {}
        missing: list[tuple[dict[str, Any], str]] = []
        waiting: dict[asyncio.Future[_Feed], dict[str, Any]] = {}
        fresh_s = max(0.0, float(self.cfg.cache_minutes)) * 60
        stale_s = max(fresh_s, float(self.cfg.max_stale_hours) * 3600)
        today = self._today(self.tz())[1]
        for row in rows:
            cal_id, url = int(row["id"]), row["url"]
            kept = self._feeds.get(cal_id)
            feed = kept[1] if kept is not None and kept[0] == url else None
            age = self._now() - feed.fetched_at if feed is not None else None
            if feed is not None and age is not None and age < fresh_s and feed.first <= today:
                feeds[cal_id] = feed
                continue
            task = self._fetch_task(cal_id, url)
            if feed is not None and age is not None and age < stale_s:
                feeds[cal_id] = feed  # answers now; the fresh read is for next time
                continue
            waiting[asyncio.shield(task)] = row
        if waiting:
            done, late = await asyncio.wait(list(waiting), timeout=float(self.cfg.timeout_seconds))
            for fut in done:
                row = waiting[fut]
                err = fut.exception() if not fut.cancelled() else CalendarError("timeout")
                if err is None:
                    feeds[int(row["id"])] = fut.result()
                else:
                    missing.append((row, err.kind if isinstance(err, CalendarError) else "bad_response"))
            for fut in late:
                missing.append((waiting[fut], "timeout"))
                fut.cancel()  # (only the shield: the read itself carries on, for next time)
            if late:
                log.info("calendar: %d calendar(s) not read within %.1f s (still reading, for next time)",
                         len(late), float(self.cfg.timeout_seconds))
        return feeds, missing

    def _fetch_task(self, cal_id: int, url: str) -> asyncio.Task[_Feed]:
        """One read per calendar at a time (callers share it); it keeps what it read, or notes why it couldn't."""
        loop = asyncio.get_running_loop()
        task = self._pending.get(cal_id)
        if task is not None and not task.done() and task.get_loop() is loop:
            return task
        task = self._pending[cal_id] = loop.create_task(self._read(url))

        def settle(t: asyncio.Task[_Feed]) -> None:
            if self._pending.get(cal_id) is t:
                del self._pending[cal_id]
            if t.cancelled():
                return
            err = t.exception()  # (retrieved: no "never retrieved" warning)
            row = self.store.get(cal_id)
            if row is None or row["url"] != url:
                return  # removed or given a new link meanwhile
            if err is None:
                feed = t.result()
                self._feeds[cal_id] = (url, feed)
                self.store.mark(cal_id, problem=None, name=feed.name, soon=self._soon(feed)[0])
            elif isinstance(err, CalendarError) and err.kind not in ("offline", "cloud_off"):
                self.store.mark(cal_id, problem=err.kind)
            else:
                log.warning("calendar: reading a calendar failed (%s)", type(err).__name__)

        task.add_done_callback(settle)
        return task

    async def _read(self, url: str) -> _Feed:
        t0 = self._now()
        data = await self._download(url)
        tz = self.tz()
        today = self._today(tz)[1]
        first, last = today - _dt.timedelta(days=WINDOW_BACK_DAYS), today + _dt.timedelta(days=WINDOW_DAYS)
        name, events = await asyncio.to_thread(read_events, data, first, last, tz)
        feed = _Feed(name, events, first, last, zlib.compress(data, 1), self._now())
        log.info("calendar: read a calendar in %d ms (%d KB, %d events in the next %d days)",
                 int((self._now() - t0) * 1000), len(data) // 1024, len(events), WINDOW_DAYS)
        return feed

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._client_loop is not loop:
            from littlebot import __version__  # noqa: PLC0415

            t = max(float(self.cfg.timeout_seconds), 10.0)
            self._client = httpx.AsyncClient(
                transport=self._transport, follow_redirects=True, max_redirects=5,
                headers={"User-Agent": f"LittleBot/{__version__}", "Accept": "text/calendar, */*;q=0.5"},
                timeout=httpx.Timeout(t, connect=min(4.0, t)))
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _download(self, url: str) -> bytes:
        chunks: list[bytes] = []
        try:
            async with self._http().stream("GET", url) as resp:
                if resp.url.scheme != "https":
                    raise CalendarError("bad_link")
                if resp.status_code in (401, 403, 404, 410):
                    raise CalendarError("not_found")
                if resp.status_code in (429, 503):
                    raise CalendarError("limited")
                if resp.status_code >= 400:
                    log.info("calendar: the calendar's server answered HTTP %d", resp.status_code)
                    raise CalendarError("bad_response")
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise CalendarError("too_big")
                    chunks.append(chunk)
        except httpx.TimeoutException:
            raise CalendarError("timeout") from None
        except httpx.TooManyRedirects:
            raise CalendarError("not_found") from None
        except httpx.HTTPError:
            raise CalendarError("network") from None
        data = b"".join(chunks)
        if b"BEGIN:VCALENDAR" not in data[:4096].lstrip(b"\xef\xbb\xbf \r\n\t").upper():
            raise CalendarError("not_calendar")
        return data


# ---------------------------------------------------------------- helpers
def _default_label(name: str) -> str:
    """The calendar's own name as its label when the parent gave none ("Calendar" when it has none either)."""
    try:
        return clean_name(name[:NAME_MAX].strip(), field="calendar name") if name else "Calendar"
    except ValueError:
        return "Calendar"


def _join(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return names[0] if names else ""
    return ", ".join(names[:-1]) + " and " + names[-1]


def _owners(rows: list[dict[str, Any]]) -> list[str]:
    """ ["Ali (Work, Personal)", "Sam (Personal)"] """
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["person"], []).append(r["label"])
    return [f"{p} ({', '.join(labels)})" for p, labels in grouped.items()]


def _start_date(ev: Event, tz: _dt.tzinfo) -> _dt.date:
    return ev.start.astimezone(tz).date() if isinstance(ev.start, _dt.datetime) else ev.start


def _starts(ev: Event, tz: _dt.tzinfo) -> _dt.datetime:
    if isinstance(ev.start, _dt.datetime):
        return ev.start
    return _dt.datetime.combine(ev.start, _dt.time(0), tz)


def _sort_key(ev: Event, tz: _dt.tzinfo) -> tuple[_dt.date, int, _dt.datetime, str]:
    return (_start_date(ev, tz), 0 if ev.all_day else 1, _starts(ev, tz), ev.title.lower())


def _merge(found: list[tuple[Event, dict[str, Any]]], tz: _dt.tzinfo) -> list[tuple[Event, list[str], list[str]]]:
    """The same event on several calendars (a family dinner both parents were invited to) is listed once."""
    merged: dict[tuple[Any, ...], tuple[Event, list[str], list[str]]] = {}
    for ev, row in found:
        key = (ev.title.lower(), _starts(ev, tz), ev.all_day,
               ev.end if not isinstance(ev.end, _dt.datetime) else ev.end.astimezone(tz))
        if key in merged:
            _, people, labels = merged[key]
            if row["person"] not in people:
                people.append(row["person"])
            labels.append(row["label"])
        else:
            merged[key] = (ev, [row["person"]], [row["label"]])
    return list(merged.values())


def _search_words(search: Any) -> list[str]:
    """ "the dentist appointment" -> ["dentist"] """
    if not search:
        return []
    return [w for w in _norm(search).split() if w not in _SEARCH_STOP and len(w) > 1]


def _matches(ev: Event, words: list[str]) -> bool:
    """A word of the search is a word of the title or place ("dentist" finds "Dentist - Dr Lee"; "swim" finds
    "Swimming"; "party" finds "Parties")."""
    have = _norm(f"{ev.title} {ev.location}").split()
    for w in words:
        for h in have:
            if h == w or h.rstrip("s") == w.rstrip("s") or (len(w) >= 4 and h.startswith(w)) or \
                    (len(w) >= 5 and h.startswith(w[:-1]) and w.endswith(("ies", "es"))):
                return True
    return False


def _event_out(ev: Event, people: list[str], labels: list[str], first: _dt.date, today: _dt.date,
               tz: _dt.tzinfo) -> dict[str, Any]:
    """One event for the model: day, date, time ("09:30-10:00" or "all day"), title, place, whose, calendar."""
    start_day = _start_date(ev, tz)
    shown = max(start_day, first)
    out: dict[str, Any] = {"day": day_label(shown, today), "date": date_text(shown)}
    if ev.all_day:
        out["time"] = "all day"
        last = ev.end - _dt.timedelta(days=1)  # type: ignore[operator]
        if last > shown:
            out["until"] = when_text(last, today)
    else:
        end_day = ev.end.date() if isinstance(ev.end, _dt.datetime) else ev.end
        out["time"] = f"{ev.start:%H:%M}" + (f"-{ev.end:%H:%M}" if ev.end != ev.start else "")
        if end_day > start_day:
            out["until"] = f"{when_text(end_day, today)} {ev.end:%H:%M}"
    if start_day < first:
        out["since"] = when_text(start_day, today)
    out["title"] = ev.title
    if ev.location:
        out["place"] = ev.location
    out["whose"] = _join(people)
    out["calendar"] = ", ".join(dict.fromkeys(labels))
    return out


def summary_text(agenda: dict[str, Any]) -> str:
    """Lines for the parent (the dashboard's "What's on today?"): "09:30-10:00 Standup (Ali, Work)"."""
    events = agenda.get("events") or []
    if not events:
        note = str(agenda.get("note") or "nothing on the calendar")
        return note[:1].upper() + note[1:] + "."
    lines = [f"{ev.get('time') or ''} {ev.get('title')} ({ev.get('whose')}, {ev.get('calendar')})".strip()
             for ev in events]
    if agenda.get("more_events"):
        lines.append(f"…and {agenda['more_events']} more.")
    return "\n".join(lines)


_KEY = "calendars"


def get_calendars(ctx: Any) -> CalendarService:
    """The one CalendarService shared by the tools and the dashboard (``ctx.extras["calendars"]``)."""
    svc = ctx.extras.get(_KEY)
    if svc is None:
        svc = ctx.extras[_KEY] = CalendarService(ctx)
    return svc


__all__ = ["CalendarError", "CalendarService", "CalendarStore", "ERRORS", "Event", "MAX_CALENDARS", "clean_name",
           "events_between", "get_calendars", "installed", "link_kind", "masked", "normalize_link", "parse_day",
           "parse_feed", "summary_text"]
