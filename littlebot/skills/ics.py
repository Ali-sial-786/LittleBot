"""Read an iCalendar (.ics) feed, such as a family calendar's "secret iCal address", and list what is on.

The caller fetches the text over HTTP itself; this module only parses and expands it:

* :func:`parse` turns the feed's text into a :class:`Feed` (its events and the calendar's name). It never raises
  on junk: a broken event is skipped with a debug log line, and at most MAX_EVENTS events are read.
* :func:`occurrences` lists every event instance that overlaps a window, sorted by start, as
  :class:`Occurrence` values (times are aware and given in the caller's zone; all-day events run from midnight
  to midnight in that zone).

Understood: folded lines, parameters (quoted too), text escapes, DTSTART / DTEND / DURATION, UTC, TZID (IANA
names, the common Windows names Outlook writes, and a VTIMEZONE's X-LIC-LOCATION), floating times and all-day
dates, STATUS:CANCELLED, RRULE (DAILY / WEEKLY / MONTHLY / YEARLY with INTERVAL, COUNT, UNTIL, BYDAY with or
without ordinals, BYMONTHDAY, BYMONTH, BYSETPOS, WKST), EXDATE, RDATE and RECURRENCE-ID overrides (moved or
cancelled instances). Repeats keep their wall-clock time across daylight-saving changes.

Not understood (ignored): VTIMEZONE rules themselves (an unknown TZID is read as the caller's zone),
HOURLY / MINUTELY / SECONDLY repeats (only the first instance is listed), BYHOUR / BYMINUTE / BYSECOND /
BYWEEKNO / BYYEARDAY, RANGE=THISANDFUTURE (an override changes only its own instance), VTODO / VJOURNAL,
alarms and attendees.

No dependencies beyond the standard library.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

log = logging.getLogger("littlebot.skills.ics")

MAX_EVENTS = 5000  # VEVENTs read from one feed; the rest are dropped with a log line
MAX_PERIODS = 50_000  # repeat periods (days, weeks, months, years) looked at per event, per call
UTC = _dt.timezone.utc

# The Windows zone names Outlook / Exchange write as TZID, mapped to IANA names.
WINDOWS_ZONES = {
    "eastern standard time": "America/Toronto",
    "us eastern standard time": "America/Indiana/Indianapolis",
    "central standard time": "America/Chicago",
    "canada central standard time": "America/Regina",
    "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "pacific standard time": "America/Los_Angeles",
    "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "atlantic standard time": "America/Halifax",
    "newfoundland standard time": "America/St_Johns",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin",
    "romance standard time": "Europe/Paris",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "e. europe standard time": "Europe/Chisinau",
    "fle standard time": "Europe/Helsinki",
    "gtb standard time": "Europe/Bucharest",
    "pakistan standard time": "Asia/Karachi",
    "india standard time": "Asia/Kolkata",
    "utc": "UTC",
    "coordinated universal time": "UTC",
    # Outlook's display names, written after "(UTC-05:00) ".
    "eastern time (us & canada)": "America/Toronto",
    "central time (us & canada)": "America/Chicago",
    "mountain time (us & canada)": "America/Denver",
    "pacific time (us & canada)": "America/Los_Angeles",
    "atlantic time (canada)": "America/Halifax",
}

_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_WANTED = frozenset({"UID", "SUMMARY", "LOCATION", "DTSTART", "DTEND", "DURATION", "STATUS", "RRULE", "EXDATE",
                     "RDATE", "RECURRENCE-ID"})
_DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
_ESCAPE = re.compile(r"\\(.)")
_UTC_PREFIX = re.compile(r"^\(?(?:UTC|GMT)[^)]*\)\s*", re.I)
_FOLD = re.compile(r"\n[ \t]")


@dataclass(frozen=True)
class Occurrence:
    uid: str
    title: str  # SUMMARY, unescaped, one line; "(no title)" if empty
    start: _dt.datetime  # timezone-aware; for all-day events midnight in `tz`
    end: _dt.datetime  # timezone-aware, exclusive
    all_day: bool
    location: str = ""
    calendar: str = ""  # X-WR-CALNAME of the feed, if any


@dataclass(frozen=True)
class _Time:
    """A DTSTART-like value as written: a date (all-day) or a wall-clock time in a zone (None = floating)."""
    value: _dt.datetime | _dt.date
    zone: _dt.tzinfo | None
    is_date: bool

    def aware(self, tz: _dt.tzinfo) -> _dt.datetime:
        v = self.value
        if self.is_date:
            return _dt.datetime(v.year, v.month, v.day, tzinfo=tz)
        return v.replace(tzinfo=self.zone or tz)  # type: ignore[union-attr]


@dataclass(frozen=True)
class _Rule:
    freq: str
    interval: int = 1
    count: int | None = None
    until: _Time | None = None
    byday: tuple[tuple[int | None, int], ...] = ()  # (ordinal or None, weekday 0 = Monday)
    bymonthday: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0


@dataclass
class Event:
    """One VEVENT, read but not yet expanded (floating times wait for the caller's zone)."""
    uid: str
    title: str
    location: str
    start: _Time
    end: _Time | None = None
    duration: _dt.timedelta | None = None
    rule: _Rule | None = None
    exdates: list[_Time] = field(default_factory=list)
    rdates: list[_Time] = field(default_factory=list)
    recurrence_id: _Time | None = None
    cancelled: bool = False

    @property
    def all_day(self) -> bool:
        return self.start.is_date


@dataclass
class Feed:
    events: list[Event] = field(default_factory=list)
    name: str = ""  # X-WR-CALNAME


# ---------------------------------------------------------------- parsing

def _unescape(text: str) -> str:
    if "\\" not in text:
        return text
    return _ESCAPE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), text)


def _one_line(text: str) -> str:
    return " ".join(_unescape(text).split())


def _split_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """``NAME;P=V;Q="a:b":value`` -> (NAME, {P: V, Q: a:b}, value); None if there is no value."""
    colon = line.find(":")
    if colon < 0:
        return None
    semi = line.find(";", 0, colon)
    if semi < 0:
        return line[:colon].upper(), {}, line[colon + 1:]
    name = line[:semi].upper()
    if '"' in line[semi:colon]:
        # A quoted parameter may hold ':' or ';': walk the characters.
        parts: list[str] = []
        cur: list[str] = []
        quoted = False
        i = semi + 1
        while i < len(line):
            ch = line[i]
            if ch == '"':
                quoted = not quoted
            elif not quoted and ch in ";:":
                parts.append("".join(cur))
                cur = []
                if ch == ":":
                    break
            else:
                cur.append(ch)
            i += 1
        else:
            return None
        value = line[i + 1:]
    else:
        parts = line[semi + 1:colon].split(";")
        value = line[colon + 1:]
    params: dict[str, str] = {}
    for part in parts:
        key, eq, val = part.partition("=")
        if eq:
            params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


class _Zones:
    """TZID -> tzinfo, cached; None when the name is unknown (the caller's zone is used then)."""

    def __init__(self) -> None:
        self.cache: dict[str, _dt.tzinfo | None] = {}
        self.aliases: dict[str, str] = {}  # TZID -> IANA name from a VTIMEZONE's X-LIC-LOCATION

    def get(self, tzid: str | None) -> _dt.tzinfo | None:
        if not tzid:
            return None
        if tzid in self.cache:
            return self.cache[tzid]
        zone = None
        name = tzid.strip().strip('"')
        candidates = [self.aliases.get(name, ""), name, name.lstrip("/")]
        candidates.append("/".join(name.split("/")[-2:]))  # "/mozilla.org/20050126_1/America/New_York"
        bare = _UTC_PREFIX.sub("", name)
        candidates.append(WINDOWS_ZONES.get(name.lower(), ""))
        candidates.append(WINDOWS_ZONES.get(bare.lower(), ""))
        for cand in candidates:
            if not cand:
                continue
            try:
                zone = ZoneInfo(cand)
                break
            except (ValueError, OSError, KeyError):  # not found / bad key
                continue
        if zone is None:
            log.debug("unknown TZID %r: using the caller's zone", tzid)
        self.cache[tzid] = zone
        return zone


def _parse_time(value: str, params: dict[str, str], zones: _Zones) -> _Time:
    """One DATE or DATE-TIME value (``20261103``, ``20261103T150000``, ``...Z``) with its TZID."""
    v = value.strip()
    if "/" in v:  # VALUE=PERIOD: keep the start
        v = v.split("/", 1)[0]
    if len(v) == 8 or params.get("VALUE", "").upper() == "DATE":
        return _Time(_dt.date(int(v[0:4]), int(v[4:6]), int(v[6:8])), None, True)
    if len(v) < 15 or v[8] not in "Tt":
        raise ValueError(f"bad date-time {value!r}")
    naive = _dt.datetime(int(v[0:4]), int(v[4:6]), int(v[6:8]), int(v[9:11]), int(v[11:13]),
                         min(int(v[13:15]), 59))
    if v[15:16] in ("Z", "z"):
        return _Time(naive, UTC, False)
    return _Time(naive, zones.get(params.get("TZID")), False)


def _parse_times(value: str, params: dict[str, str], zones: _Zones) -> list[_Time]:
    out = []
    for part in value.split(","):
        if part.strip():
            try:
                out.append(_parse_time(part, params, zones))
            except (ValueError, IndexError):
                log.debug("skipping bad time %r", part)
    return out


def _parse_duration(value: str) -> _dt.timedelta:
    m = _DURATION.match(value.strip().upper())
    if not m or not any(m.groups()[1:]):
        raise ValueError(f"bad duration {value!r}")
    sign, w, d, h, mi, s = m.groups()
    delta = _dt.timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0),
                          seconds=int(s or 0))
    return -delta if sign == "-" else delta


def _parse_rule(value: str, zones: _Zones) -> _Rule | None:
    parts: dict[str, str] = {}
    for part in value.split(";"):
        key, _, val = part.partition("=")
        parts[key.strip().upper()] = val.strip().upper()
    freq = parts.get("FREQ", "")
    if freq not in _FREQS:
        log.debug("unsupported RRULE %r: listing only the first instance", value)
        return None

    def ints(key: str) -> tuple[int, ...]:
        return tuple(int(x) for x in parts.get(key, "").split(",") if x.strip())

    byday = []
    for item in parts.get("BYDAY", "").split(","):
        item = item.strip()
        if len(item) >= 2 and item[-2:] in _DAYS:
            ordinal = item[:-2]
            byday.append((int(ordinal) if ordinal not in ("", "+", "-") else None, _DAYS[item[-2:]]))
    until = None
    if parts.get("UNTIL"):
        until = _parse_time(parts["UNTIL"], {}, zones)
    count = int(parts["COUNT"]) if parts.get("COUNT") else None
    return _Rule(freq=freq, interval=max(1, int(parts.get("INTERVAL") or 1)), count=count, until=until,
                 byday=tuple(byday), bymonthday=ints("BYMONTHDAY"),
                 bymonth=tuple(m for m in ints("BYMONTH") if 1 <= m <= 12), bysetpos=ints("BYSETPOS"),
                 wkst=_DAYS.get(parts.get("WKST", "MO"), 0))


def _build_event(props: list[tuple[str, dict[str, str], str]], zones: _Zones, index: int) -> Event | None:
    first: dict[str, tuple[dict[str, str], str]] = {}
    exdates: list[_Time] = []
    rdates: list[_Time] = []
    for name, params, value in props:
        if name == "EXDATE":
            exdates.extend(_parse_times(value, params, zones))
        elif name == "RDATE":
            rdates.extend(_parse_times(value, params, zones))
        elif name not in first:
            first[name] = (params, value)
    if "DTSTART" not in first:
        return None
    start = _parse_time(first["DTSTART"][1], first["DTSTART"][0], zones)
    event = Event(uid=first.get("UID", ({}, ""))[1].strip() or f"no-uid-{index}",
                  title=_one_line(first.get("SUMMARY", ({}, ""))[1]) or "(no title)",
                  location=_one_line(first.get("LOCATION", ({}, ""))[1]), start=start,
                  exdates=exdates, rdates=rdates,
                  cancelled=first.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED")
    if "DTEND" in first:
        try:
            event.end = _parse_time(first["DTEND"][1], first["DTEND"][0], zones)
        except (ValueError, IndexError):
            log.debug("bad DTEND in %s", event.uid)
    elif "DURATION" in first:
        try:
            event.duration = _parse_duration(first["DURATION"][1])
        except ValueError:
            log.debug("bad DURATION in %s", event.uid)
    if "RRULE" in first:
        try:
            event.rule = _parse_rule(first["RRULE"][1], zones)
        except (ValueError, IndexError):
            log.debug("bad RRULE in %s: listing only the first instance", event.uid)
    if "RECURRENCE-ID" in first:
        event.recurrence_id = _parse_time(first["RECURRENCE-ID"][1], first["RECURRENCE-ID"][0], zones)
    return event


def parse(text: str, since: _dt.date | None = None) -> Feed:
    """Read a feed's text. Never raises: bad events are skipped; at most MAX_EVENTS are kept.

    ``since``: one-off events that ended before that day are skipped (repeating events and their changes are all
    kept). A calendar keeps every event since it was made: without this, a busy one's old events could fill
    MAX_EVENTS and the coming ones be dropped."""
    feed = Feed()
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        if not isinstance(text, str):
            return feed
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _FOLD.sub("", text)
    except Exception:  # noqa: BLE001 - junk in, empty feed out
        log.debug("unreadable feed", exc_info=True)
        return feed

    zones = _Zones()
    stack: list[str] = []
    props: list[tuple[str, dict[str, str], str]] | None = None
    tzid = ""
    count = 0
    for line in text.split("\n"):
        if not line:
            continue
        head = line[:6].upper()
        if head == "BEGIN:":
            comp = line[6:].strip().upper()
            stack.append(comp)
            if comp == "VEVENT" and len(stack) <= 2:
                props = []
            continue
        if head[:4] == "END:":
            comp = line[4:].strip().upper()
            if comp in stack:
                while stack and stack.pop() != comp:
                    pass
            if comp == "VEVENT" and props is not None:
                count += 1
                try:
                    event = _build_event(props, zones, count)
                    if event is not None and not (since is not None and _over_before(event, since)):
                        feed.events.append(event)
                except Exception:  # noqa: BLE001 - one bad event never spoils the feed
                    log.debug("skipping a bad event", exc_info=True)
                props = None
                if len(feed.events) >= MAX_EVENTS:
                    log.info("ics feed has more than %d events: the rest are dropped", MAX_EVENTS)
                    break
            continue
        if not stack:
            continue
        top = stack[-1]
        if top == "VEVENT" and props is not None:
            # Cheap name check first: most lines (DESCRIPTION, ATTENDEE, ...) are not wanted.
            cut = len(line)
            for sep in (";", ":"):
                j = line.find(sep)
                if 0 <= j < cut:
                    cut = j
            if line[:cut].upper() not in _WANTED:
                continue
            split = _split_line(line)
            if split is not None:
                props.append(split)
        elif top == "VCALENDAR":
            if line[:13].upper() in ("X-WR-CALNAME:", "X-WR-CALNAME;"):
                split = _split_line(line)
                if split is not None:
                    feed.name = _one_line(split[2])
        elif top == "VTIMEZONE":
            split = _split_line(line)
            if split is None:
                continue
            if split[0] == "TZID":
                tzid = split[2].strip()
            elif split[0] == "X-LIC-LOCATION" and tzid:
                zones.aliases[tzid] = split[2].strip()
    return feed


def _over_before(event: Event, day: _dt.date) -> bool:
    """A one-off event that ended before ``day`` (by its own date; callers leave a day or two to spare)."""
    if event.rule is not None or event.rdates or event.recurrence_id is not None:
        return False
    end = event.end.value if event.end is not None else event.start.value
    if event.end is None and event.duration is not None:
        end = end + event.duration
    last = end.date() if isinstance(end, _dt.datetime) else end
    return last < day


# ---------------------------------------------------------------- expansion

def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def _days_in_month(year: int, month: int) -> int:
    y, m = _add_months(year, month, 1)
    return (_dt.date(y, m, 1) - _dt.timedelta(days=1)).day


def _nth_weekdays(days: list[_dt.date], byday: tuple[tuple[int | None, int], ...]) -> set[_dt.date]:
    """The dates in ``days`` (one month or year, in order) matching BYDAY: 2MO = 2nd Monday, -1FR = last Friday."""
    out: set[_dt.date] = set()
    for ordinal, wd in byday:
        same = [d for d in days if d.weekday() == wd]
        if ordinal is None:
            out.update(same)
        elif 0 < ordinal <= len(same):
            out.add(same[ordinal - 1])
        elif ordinal < 0 and -ordinal <= len(same):
            out.add(same[ordinal])
    return out


def _month_dates(rule: _Rule, year: int, month: int, default_day: int) -> list[_dt.date]:
    n = _days_in_month(year, month)
    if rule.bymonthday:
        days = {d if d > 0 else n + d + 1 for d in rule.bymonthday}
        dates = {_dt.date(year, month, d) for d in days if 1 <= d <= n}
        if rule.byday:
            wds = {wd for _, wd in rule.byday}
            dates = {d for d in dates if d.weekday() in wds}
        return sorted(dates)
    if rule.byday:
        return sorted(_nth_weekdays([_dt.date(year, month, d) for d in range(1, n + 1)], rule.byday))
    return [_dt.date(year, month, default_day)] if default_day <= n else []


def _period(rule: _Rule, d0: _dt.date, k: int) -> tuple[_dt.date, list[_dt.date]]:
    """Period ``k`` of the rule (k * INTERVAL days / weeks / months / years after DTSTART's): its first day and its
    candidate dates, sorted, after BYSETPOS."""
    step = k * rule.interval
    if rule.freq == "DAILY":
        first = d0 + _dt.timedelta(days=step)
        dates = [first]
        if rule.bymonthday:
            n = _days_in_month(first.year, first.month)
            if not any(first.day == (d if d > 0 else n + d + 1) for d in rule.bymonthday):
                dates = []
        if rule.byday and first.weekday() not in {wd for _, wd in rule.byday}:
            dates = []
    elif rule.freq == "WEEKLY":
        first = d0 - _dt.timedelta(days=(d0.weekday() - rule.wkst) % 7) + _dt.timedelta(weeks=step)
        wds = {wd for _, wd in rule.byday} or {d0.weekday()}
        dates = [first + _dt.timedelta(days=i) for i in range(7) if (first + _dt.timedelta(days=i)).weekday() in wds]
    elif rule.freq == "MONTHLY":
        y, m = _add_months(d0.year, d0.month, step)
        first = _dt.date(y, m, 1)
        dates = _month_dates(rule, y, m, d0.day) if not rule.bymonth or m in rule.bymonth else []
    else:  # YEARLY
        y = d0.year + step
        first = _dt.date(y, 1, 1)
        if not rule.byday and not rule.bymonthday:
            dates = [d for m in (rule.bymonth or (d0.month,)) for d in _month_dates(rule, y, m, d0.day)]
        elif rule.bymonth or rule.bymonthday:
            dates = [d for m in (rule.bymonth or range(1, 13)) for d in _month_dates(rule, y, m, d0.day)]
        else:  # BYDAY alone: ordinals count through the whole year (20MO = 20th Monday)
            n = (_dt.date(y + 1, 1, 1) - first).days
            dates = sorted(_nth_weekdays([first + _dt.timedelta(days=i) for i in range(n)], rule.byday))
    if rule.bymonth and rule.freq in ("DAILY", "WEEKLY"):
        dates = [d for d in dates if d.month in rule.bymonth]
    if rule.bysetpos and dates:
        picked = {dates[p - 1 if p > 0 else p] for p in rule.bysetpos if 0 < abs(p) <= len(dates) and p != 0}
        dates = sorted(picked)
    return first, dates


def _jump(rule: _Rule, d0: _dt.date, target: _dt.date) -> int:
    """The period index a little before ``target``, so a years-old weekly event doesn't walk every week."""
    if target <= d0:
        return 0
    if rule.freq == "DAILY":
        units = (target - d0).days
    elif rule.freq == "WEEKLY":
        week0 = d0 - _dt.timedelta(days=(d0.weekday() - rule.wkst) % 7)
        units = (target - week0).days // 7
    elif rule.freq == "MONTHLY":
        units = (target.year - d0.year) * 12 + target.month - d0.month
    else:
        units = target.year - d0.year
    return max(0, units // rule.interval - 1)


def _rule_starts(rule: _Rule, start: _dt.datetime, until: _dt.datetime | None, stop: _dt.datetime,
                 jump_to: _dt.date, cap: int) -> list[_dt.datetime]:
    """Wall-clock starts the rule makes after DTSTART (DTSTART itself is added by the caller), from ``jump_to`` up
    to ``stop``, at most ``cap``.

    With COUNT the walk begins at DTSTART so the count is right (capped at MAX_PERIODS); without it, it jumps to
    just before ``jump_to``.
    """
    d0, t0 = start.date(), start.time()
    k = 0 if rule.count is not None else _jump(rule, d0, jump_to)
    made = 1  # DTSTART is the first instance, as COUNT counts it
    out: list[_dt.datetime] = []
    stop_day = stop.date() + _dt.timedelta(days=1)
    for _ in range(MAX_PERIODS):
        try:
            first, dates = _period(rule, d0, k)
        except (ValueError, OverflowError):  # past year 9999
            break
        if first > stop_day:
            break
        k += 1
        for d in dates:
            at = _dt.datetime.combine(d, t0)
            if at <= start:
                continue
            if (until is not None and at > until) or at > stop:
                return out
            if rule.count is not None:
                if made >= rule.count:
                    return out
                made += 1
            if d < jump_to:  # counted, but long before the window
                continue
            out.append(at)
            if len(out) >= cap:
                return out
    return out


def _local(t: _Time, zone: _dt.tzinfo, tz: _dt.tzinfo, all_day: bool) -> _dt.datetime:
    """A value as a naive wall-clock time in the event's zone (midnight for dates)."""
    if t.is_date:
        v = t.value
        return _dt.datetime(v.year, v.month, v.day)
    aware = t.aware(tz).astimezone(zone)
    if all_day:
        return _dt.datetime(aware.year, aware.month, aware.day)
    return aware.replace(tzinfo=None)


def _matcher(times: list[_Time], tz: _dt.tzinfo) -> tuple[set[_dt.datetime], set[_dt.date]]:
    """EXDATE / RECURRENCE-ID values as UTC instants (date-times) and dates (DATE values)."""
    instants: set[_dt.datetime] = set()
    dates: set[_dt.date] = set()
    for t in times:
        if t.is_date:
            dates.add(t.value if not isinstance(t.value, _dt.datetime) else t.value.date())
        else:
            instants.add(t.aware(tz).astimezone(UTC))
    return instants, dates


def _expand(event: Event, win_start: _dt.datetime, win_end: _dt.datetime, tz: _dt.tzinfo, limit: int,
            skip: list[_Time], calendar: str) -> list[Occurrence]:
    all_day = event.all_day
    zone = tz if all_day else (event.start.zone or tz)
    start = _local(event.start, zone, tz, all_day)

    # Length: whole days for all-day events (kept across DST), an exact span otherwise.
    days = 1
    span = _dt.timedelta(0)
    if all_day:
        if event.end is not None:
            end_v = event.end.value
            end_d = end_v.date() if isinstance(end_v, _dt.datetime) else end_v
            days = (end_d - start.date()).days
        elif event.duration is not None:
            days = event.duration.days + (1 if event.duration.seconds else 0)
        days = max(days, 1)
        span = _dt.timedelta(days=days)
    elif event.end is not None:
        span = event.end.aware(zone).astimezone(UTC) - start.replace(tzinfo=zone).astimezone(UTC)
    elif event.duration is not None:
        span = event.duration
    span = max(span, _dt.timedelta(0))

    starts = [start]
    if event.rule is not None or event.rdates:
        stop = win_end.astimezone(zone).replace(tzinfo=None)
        jump_to = (win_start.astimezone(zone).replace(tzinfo=None) - span - _dt.timedelta(days=2)).date()
        if event.rule is not None:
            until = None
            if event.rule.until is not None:
                u = event.rule.until
                if u.is_date:  # the whole UNTIL day is in
                    until = _dt.datetime.combine(u.value, _dt.time(23, 59, 59))
                else:
                    until = u.aware(zone).astimezone(zone).replace(tzinfo=None)
            starts += _rule_starts(event.rule, start, until, stop, jump_to, limit + len(skip) + len(event.exdates))
        starts += [_local(t, zone, tz, all_day) for t in event.rdates]
        starts = sorted(set(starts))

    ex_instants, ex_dates = _matcher(event.exdates + skip, tz)
    out: list[Occurrence] = []
    for at in starts:
        if all_day:
            s = _dt.datetime(at.year, at.month, at.day, tzinfo=tz)
            e = s.replace(tzinfo=None) + _dt.timedelta(days=days)
            e = e.replace(tzinfo=tz)
            key = s.astimezone(UTC)
        else:
            key = at.replace(tzinfo=zone).astimezone(UTC)
            s = key.astimezone(tz)
            e = (key + span).astimezone(tz)
        if key in ex_instants or at.date() in ex_dates:
            continue
        if s >= win_end:
            continue
        if e > win_start or (e == s and s >= win_start):
            out.append(Occurrence(uid=event.uid, title=event.title, start=s, end=e, all_day=all_day,
                                  location=event.location, calendar=calendar))
            if len(out) >= limit:
                break
    return out


def occurrences(feed: Feed, start: _dt.datetime, end: _dt.datetime, tz: _dt.tzinfo, *,
                limit: int = 500) -> list[Occurrence]:
    """Every event instance overlapping [start, end), sorted by start, at most `limit`."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    if end <= start or limit <= 0:
        return []
    # RECURRENCE-ID overrides, by UID: they replace (or, when cancelled, remove) one instance of the series.
    overrides: dict[str, list[_Time]] = {}
    for ev in feed.events:
        if ev.recurrence_id is not None:
            overrides.setdefault(ev.uid, []).append(ev.recurrence_id)
    out: list[Occurrence] = []
    for ev in feed.events:
        if ev.cancelled:
            continue
        try:
            if ev.recurrence_id is not None:
                single = Event(uid=ev.uid, title=ev.title, location=ev.location, start=ev.start, end=ev.end,
                               duration=ev.duration)
                out += _expand(single, start, end, tz, limit, [], feed.name)
            else:
                out += _expand(ev, start, end, tz, limit, overrides.get(ev.uid, []), feed.name)
        except Exception:  # noqa: BLE001 - one odd event never spoils the list
            log.debug("skipping event %s while expanding", ev.uid, exc_info=True)
    out.sort(key=lambda o: (o.start, o.end, o.title))
    return out[:limit]
