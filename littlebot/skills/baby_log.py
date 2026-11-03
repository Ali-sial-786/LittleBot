"""Baby log: feeds, diapers, sleep and medicine by voice ("she just had 4 ounces", "dirty diaper", "he's down for a
nap", "she's awake", "when did she last eat?", "how many diapers today?").

Entries live in SQLite on the robot (``skill_baby_log``) for ``KEEP_DAYS`` days. The brain's context gets one line
each turn with the last feed, diaper and sleep and how long ago, so "when did she last eat?" needs no tool call;
``baby_summary`` adds up a day (feeds and how much, diapers wet and dirty, sleep). "At" is when it happened: an
ISO local time from the model, or "20 minutes ago" / "at 2:30" (the latest such time that has passed). Logging only:
it never suggests a dose or gives medical advice.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import threading
from typing import Any, Callable

from littlebot.info.clock import local_zone_name
from littlebot.info.clock import zone as clock_zone
from littlebot.skills import when as W
from littlebot.skills.base import Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.baby_log")

TABLE = "skill_baby_log"
KINDS = ("feed", "diaper", "sleep", "wake", "medicine", "note")  # "wake" ends a sleep
KEEP_DAYS = 60
MAX_TEXT = 80
MAX_AGO_S = 2 * 86400  # an entry for further back than this is refused (a misheard time)
NOTE_HOURS = 24  # the context line covers this long
UNITS = {"oz": "oz", "ounce": "oz", "ounces": "oz", "fl oz": "oz", "ml": "ml", "mls": "ml", "millilitre": "ml",
         "millilitres": "ml", "milliliter": "ml", "milliliters": "ml", "cc": "ml", "min": "min", "mins": "min",
         "minute": "min", "minutes": "min", "mg": "mg", "g": "g", "grams": "g", "drops": "drops", "drop": "drops"}
_DIRTY = re.compile(r"\b(?:dirty|poo+p?y?|poo|bm|both|mixed|messy|stool)\b", re.I)
_WET = re.compile(r"\b(?:wet|pee|wee|both|mixed)\b", re.I)
_NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
                 "thirty": 30, "forty": 40, "forty five": 45, "forty-five": 45, "fifty": 50}
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + ")"
_AGO = re.compile(rf"^(?:about\s+|around\s+)?(?:{_NUM}\s*(hours?|hrs?|h)\b)?\s*(?:and\s+)?(?:{_NUM}\s*"
                  r"(minutes?|mins?|m)\b)?\s*ago$")
_CLOCK = re.compile(r"^(?:at\s+|around\s+|about\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m?\.?$|"
                    r"^(?:at\s+|around\s+|about\s+)?(\d{1,2})(?::(\d{2}))?$")
AT_HINT = "give when it happened as an ISO local time like 2026-11-03T14:10, or 'now', '20 minutes ago', 'at 2:30 pm'"


class BadTime(ValueError):
    """``at`` couldn't be understood, is in the future or too long ago: ``str(e)`` says why, for the model."""


def _number(word: str) -> float:
    return float(_NUMBER_WORDS.get(word, word))


def past_time(value: Any, now: _dt.datetime) -> _dt.datetime:
    """When ``value`` (from the model) says it happened, at or before ``now`` (aware, the home's zone)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return now
    if not isinstance(value, str):
        raise BadTime(AT_HINT)
    raw = " ".join(value.split())
    s = raw.lower().strip(" .")
    if s in ("now", "just now", "right now", "just", "a moment ago", "a minute ago", "just a minute ago"):
        return now
    if s in ("half an hour ago", "half hour ago", "a half hour ago"):
        return now - _dt.timedelta(minutes=30)
    if s in ("an hour and a half ago", "one and a half hours ago", "1.5 hours ago"):
        return now - _dt.timedelta(minutes=90)
    at: _dt.datetime | None = None
    try:
        parsed = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        at = parsed.replace(tzinfo=now.tzinfo) if parsed.tzinfo is None else parsed.astimezone(now.tzinfo)
    elif (m := _AGO.match(s)) and (m.group(1) or m.group(3)):
        minutes = (_number(m.group(1)) * 60 if m.group(1) else 0.0) + (_number(m.group(3)) if m.group(3) else 0.0)
        at = now - _dt.timedelta(minutes=minutes)
    elif m := _CLOCK.match(s):
        hour = int(m.group(1) or m.group(4))
        minute = int(m.group(2) or m.group(5) or 0)
        ampm = m.group(3)
        if minute > 59 or hour > 23 or (ampm and not 1 <= hour <= 12):
            raise BadTime(f"{raw!r} isn't a time of day; {AT_HINT}")
        hours = [hour % 12 + (12 if ampm == "p" else 0)] if ampm else ([hour, hour + 12] if hour < 12 else [hour])
        candidates = [W.local_dt(now.date() - _dt.timedelta(days=d), h, minute, now.tzinfo)
                      for d in (0, 1) for h in hours]
        passed = [c for c in candidates if c <= now + _dt.timedelta(minutes=2)]
        at = max(passed) if passed else None
    if at is None:
        raise BadTime(AT_HINT)
    if at > now + _dt.timedelta(minutes=5):
        raise BadTime(f"{raw!r} is still to come; log things once they have happened")
    if (now - at).total_seconds() > MAX_AGO_S:
        raise BadTime(f"{raw!r} is more than 2 days ago; {AT_HINT}")
    return min(at, now)


def ago_words(seconds: float) -> str:
    """ "just now", "25 minutes ago", "1 hour 20 minutes ago", "5 hours ago"."""
    m = int(max(0, seconds) // 60)
    if m < 2:
        return "just now"
    if m < 60:
        return f"{m} minutes ago"
    h, m = divmod(m, 60)
    hours = f"{h} hour{'s' if h != 1 else ''}"
    return f"{hours} {m} minute{'s' if m != 1 else ''} ago" if m and h < 5 else f"{hours} ago"


def span_words(seconds: float) -> str:
    """ "45 minutes", "2 hours 10 minutes"."""
    m = int(max(0, seconds) // 60)
    h, m = divmod(m, 60)
    if not h:
        return f"{m} minute{'s' if m != 1 else ''}"
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minute{'s' if m != 1 else ''}" if m else "")


def _unit(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return UNITS.get(" ".join(value.lower().split()).strip("."), "")


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    s = " ".join(str(value).split()).strip(" \"'.,;:!?")
    return s[:MAX_TEXT].rsplit(" ", 1)[0] if len(s) > MAX_TEXT else s


def _amount(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if 0 < v < 10000 else None


def _num(v: float) -> str:
    return f"{v:g}"


class BabyLog(Skill):
    name = "baby_log"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._ready = False
        self._lock = threading.RLock()
        self._zone: _dt.tzinfo | None | bool = False

    def available(self) -> bool:
        return bool(getattr(getattr(self.ctx.config, "skills", None), "baby_log", True))

    # ------------------------------------------------------------ time
    def zone(self) -> _dt.tzinfo | None:
        """The home's time zone (``info.timezone``, else the system's by name); None: the robot's local time."""
        if self._zone is False:
            name = str(getattr(getattr(self.ctx.config, "info", None), "timezone", "") or "").strip()
            tz: _dt.tzinfo | None = None
            for candidate in (name, local_zone_name() or ""):
                try:
                    tz = clock_zone(candidate)
                except ValueError:
                    continue
                if tz is not None:
                    break
            self._zone = tz
        return self._zone if self._zone is not False else None  # type: ignore[return-value]

    def local(self, ts: float | None = None) -> _dt.datetime:
        return W.now_local(self.now() if ts is None else ts, self.zone())

    def _clock(self, ts: float) -> str:
        at, now = self.local(ts), self.local()
        when = W.clock_words(at)
        return when if at.date() == now.date() else f"{when} yesterday" if (now.date() - at.date()).days == 1 \
            else f"{when} on {at:%A}"

    # ------------------------------------------------------------ storage
    def _db(self) -> Any:
        db = self.ctx.db
        if not self._ready:
            db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
                       "at REAL NOT NULL, amount REAL, unit TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', "
                       "added REAL NOT NULL)")
            db.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_at ON {TABLE}(at)")
            self._ready = True
        return db

    def entries(self, since: float, kind: str | None = None) -> list[dict[str, Any]]:
        """Entries at or after ``since`` (wall-clock seconds), oldest first."""
        with self._lock:
            if kind:
                return self._db().query(f"SELECT * FROM {TABLE} WHERE at>=? AND kind=? ORDER BY at, id",
                                        (since, kind))
            return self._db().query(f"SELECT * FROM {TABLE} WHERE at>=? ORDER BY at, id", (since,))

    def last(self, *kinds: str) -> dict[str, Any] | None:
        marks = ",".join("?" * len(kinds))
        with self._lock:
            return self._db().query_one(f"SELECT * FROM {TABLE} WHERE kind IN ({marks}) ORDER BY at DESC, id DESC "
                                        "LIMIT 1", kinds)

    def add(self, kind: str, at: float, *, amount: float | None = None, unit: str = "", detail: str = "") -> int:
        with self._lock:
            db = self._db()
            db.execute(f"DELETE FROM {TABLE} WHERE at<?", (self.now() - KEEP_DAYS * 86400,))
            db.execute(f"INSERT INTO {TABLE}(kind, at, amount, unit, detail, added) VALUES(?, ?, ?, ?, ?, ?)",
                       (kind, at, amount, unit, detail, self.now()))
            row = db.query_one(f"SELECT MAX(id) AS id FROM {TABLE}")
        return int(row["id"]) if row else 0

    def remove(self, entry_id: int) -> None:
        with self._lock:
            self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (int(entry_id),))

    # ------------------------------------------------------------ words
    def describe(self, row: dict[str, Any]) -> str:
        """ "a feed (4 oz, bottle)", "a diaper (dirty)", "medicine (Tylenol, 2.5 ml)", "sleep", "a note: ..."."""
        kind, detail = str(row["kind"]), str(row.get("detail") or "")
        amount = f"{_num(float(row['amount']))} {row['unit']}".strip() if row.get("amount") is not None else ""
        bits = ", ".join(b for b in ((amount, detail) if kind == "feed" else (detail, amount)) if b)
        if kind == "note":
            return f"a note: {detail}" if detail else "a note"
        if kind == "medicine":
            return f"medicine ({bits})" if bits else "medicine"
        if kind in ("sleep", "wake"):
            return {"sleep": "asleep", "wake": "awake"}[kind] + (f" ({bits})" if bits else "")
        return f"a {kind}" + (f" ({bits})" if bits else "")

    def sleep_now(self) -> tuple[bool, dict[str, Any] | None]:
        """(asleep now, the entry that started or ended the last sleep)."""
        row = self.last("sleep", "wake")
        return (row is not None and row["kind"] == "sleep"), row

    def day(self, since: float, until: float) -> dict[str, Any]:
        """The totals between ``since`` and ``until``."""
        rows = [r for r in self.entries(since) if float(r["at"]) <= until]
        feeds = [r for r in rows if r["kind"] == "feed"]
        diapers = [r for r in rows if r["kind"] == "diaper"]
        amounts: dict[str, float] = {}
        for r in feeds:
            if r.get("amount") is not None and r.get("unit") in ("oz", "ml", "min"):
                amounts[str(r["unit"])] = amounts.get(str(r["unit"]), 0.0) + float(r["amount"])
        slept = 0.0
        start: float | None = None
        before = self._db().query_one(f"SELECT kind, at FROM {TABLE} WHERE at<? AND kind IN ('sleep','wake') ORDER BY "
                                      "at DESC, id DESC LIMIT 1", (since,))
        if before is not None and before["kind"] == "sleep":
            start = since  # asleep when the day began
        for r in rows:
            if r["kind"] == "sleep" and start is None:
                start = float(r["at"])
            elif r["kind"] == "wake" and start is not None:
                slept += float(r["at"]) - start
                start = None
        if start is not None:
            slept += until - start
        return {"feeds": len(feeds), "amounts": amounts, "diapers": len(diapers),
                "dirty": sum(1 for r in diapers if _DIRTY.search(str(r.get("detail") or ""))),
                "wet": sum(1 for r in diapers if _WET.search(str(r.get("detail") or ""))),
                "medicine": [r for r in rows if r["kind"] == "medicine"], "slept_s": slept,
                "notes": [r for r in rows if r["kind"] == "note"]}

    def day_words(self, totals: dict[str, Any]) -> str:
        parts = []
        if totals["feeds"]:
            amounts = ", ".join(f"{_num(round(totals['amounts'][u], 1))} {u}" for u in ("oz", "ml", "min")
                                if u in totals["amounts"])
            parts.append(f"{totals['feeds']} feed{'s' if totals['feeds'] != 1 else ''}"
                         + (f" ({amounts} in all)" if amounts else ""))
        if totals["diapers"]:
            kinds = ", ".join(f"{n} {k}" for k, n in (("wet", totals["wet"]), ("dirty", totals["dirty"])) if n)
            parts.append(f"{totals['diapers']} diaper{'s' if totals['diapers'] != 1 else ''}"
                         + (f" ({kinds})" if kinds else ""))
        if totals["slept_s"] >= 60:
            parts.append(f"slept {span_words(totals['slept_s'])}")
        for r in totals["medicine"]:
            parts.append(f"{self.describe(r)} at {self._clock(float(r['at']))}")
        if totals["notes"]:
            parts.append(f"{len(totals['notes'])} note{'s' if len(totals['notes']) != 1 else ''}")
        return "; ".join(parts) if parts else "nothing logged"

    # ------------------------------------------------------------ for the model
    def tools(self) -> list[ToolSpec]:
        at = {"type": "string", "description": "When it happened (not needed for just now): an ISO local time like "
                                               "2026-11-03T14:10, or '20 minutes ago', 'at 2:30 pm'."}
        return [
            tool("log_baby", "Log something about the baby: a feed (bottle, breast or solids), a diaper, falling "
                 "asleep, waking up, medicine given, or a short note.", {
                     "kind": {"type": "string", "enum": list(KINDS)},
                     "detail": {"type": "string", "description": "Short: 'bottle', 'formula', 'breast left', "
                                "'solids', 'wet', 'dirty', 'both', the medicine's name, or the note."},
                     "amount": {"type": "number", "description": "How much, only if said (4, 120, 15)."},
                     "unit": {"type": "string", "enum": ["oz", "ml", "min", "mg", "g", "drops"],
                              "description": "The amount's unit: oz or ml for a bottle, min for breastfeeding."},
                     "at": at}, ("kind",)),
            tool("baby_summary", "The baby's day: feeds and how much, diapers (wet / dirty), sleep, medicine. Today "
                 "by default; 'yesterday' or the last N hours.", {
                     "day": {"type": "string", "enum": ["today", "yesterday"]},
                     "hours": {"type": "integer", "minimum": 1, "maximum": 48,
                               "description": "The last N hours instead of a day."},
                     "kind": {"type": "string", "enum": list(KINDS), "description": "Only this kind's entries."}}),
            tool("undo_baby_log", "Take the last baby-log entry off (or the last of one kind), when they say it was "
                 "wrong or logged twice.", {"kind": {"type": "string", "enum": list(KINDS)}}),
        ]

    def prompt_note(self) -> str:
        return ("Baby log: when they say the baby ate, had a diaper changed, fell asleep or woke up, had medicine, or "
                "something to note, log it with log_baby (kind; detail, amount and unit only if said; at only if it "
                "wasn't just now). One sentence back ('Logged: 4 ounces at 2:10.'). The context's 'Baby log' line "
                "answers 'when did she last eat?' without a tool; baby_summary adds up a day. undo_baby_log when "
                "an entry was wrong. Only log what they tell you: never suggest a dose or give medical advice from "
                "the log (that's for their doctor).")

    def context_note(self) -> str:
        if not self.available():
            return ""
        try:
            now = self.now()
            rows = self.entries(now - NOTE_HOURS * 3600)
            asleep, sleep_row = self.sleep_now()
        except Exception:  # noqa: BLE001
            return ""
        if not rows and not asleep:
            return ""
        parts = []
        for kind, label in (("feed", "last feed"), ("diaper", "last diaper"), ("medicine", "last medicine")):
            row = next((r for r in reversed(rows) if r["kind"] == kind), None)
            if row is not None:
                at = float(row["at"])
                bits = self.describe(row).split(" (", 1)
                extra = f" ({bits[1]}" if len(bits) > 1 else ""
                parts.append(f"{label} {self._clock(at)}, {ago_words(now - at)}{extra}")
        if asleep and sleep_row is not None:
            parts.append(f"asleep since {self._clock(float(sleep_row['at']))} "
                         f"({span_words(now - float(sleep_row['at']))})")
        elif sleep_row is not None and now - float(sleep_row["at"]) < NOTE_HOURS * 3600:
            parts.append(f"awake since {self._clock(float(sleep_row['at']))}")
        return "Baby log: " + "; ".join(parts) + "." if parts else ""

    def status(self) -> dict[str, Any]:
        try:
            start = self.local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            today = len(self.entries(start))
        except Exception:  # noqa: BLE001
            today = 0
        return {**super().status(), "today": today}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if not self.available():
            return err("the baby log is switched off")
        if name == "log_baby":
            return self._log(args)
        if name == "baby_summary":
            return self._summary(args)
        if name == "undo_baby_log":
            return self._undo(args.get("kind"))
        return err(f"baby_log has no tool called {str(name)[:40]}")

    def _log(self, args: dict[str, Any]) -> str:
        kind = str(args.get("kind") or "").strip().lower()
        if kind not in KINDS:
            return err(f"kind must be one of {', '.join(KINDS)}")
        try:
            at = past_time(args.get("at"), self.local())
        except BadTime as e:
            return err(str(e))
        detail = _text(args.get("detail"))
        if kind == "note" and not detail:
            return err("give the note's words in detail")
        amount, unit = _amount(args.get("amount")), _unit(args.get("unit"))
        if amount is None:
            unit = ""
        ts = at.timestamp()
        self.add(kind, ts, amount=amount, unit=unit, detail=detail)
        log.info("baby log: a %s logged", kind)
        entry = self.describe({"kind": kind, "amount": amount, "unit": unit, "detail": detail})
        if kind in ("sleep", "wake"):
            entry = {"sleep": "falling asleep", "wake": "waking up"}[kind] + (f" ({detail})" if detail else "")
        words = f"logged {entry} at {self._clock(ts)}"
        if kind == "wake":
            asleep_row = self._db().query_one(f"SELECT at FROM {TABLE} WHERE kind='sleep' AND at<? ORDER BY at DESC "
                                              "LIMIT 1", (ts,))
            woke_row = self._db().query_one(f"SELECT at FROM {TABLE} WHERE kind='wake' AND at<? ORDER BY at DESC "
                                            "LIMIT 1", (ts,))
            if asleep_row is not None and (woke_row is None or float(woke_row["at"]) < float(asleep_row["at"])):
                words += f" (slept {span_words(ts - float(asleep_row['at']))})"
        if kind in ("feed", "diaper"):
            start = self.local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            totals = self.day(start, self.now())
            words += f"; today: {self.day_words({**totals, 'medicine': [], 'notes': [], 'slept_s': 0})}"
        return ok(words)

    def _summary(self, args: dict[str, Any]) -> str:
        now = self.local()
        hours = args.get("hours")
        if isinstance(hours, (int, float)) and not isinstance(hours, bool) and 1 <= hours <= 48:
            since, until, label = now.timestamp() - float(hours) * 3600, now.timestamp(), f"the last {int(hours)} hours"
        elif args.get("day") == "yesterday":
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            since = (midnight - _dt.timedelta(days=1)).timestamp()
            until, label = midnight.timestamp(), "yesterday"
        else:
            since = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            until, label = now.timestamp(), "today so far"
        kind = str(args.get("kind") or "").strip().lower()
        if kind:
            if kind not in KINDS:
                return err(f"kind must be one of {', '.join(KINDS)}")
            rows = [r for r in self.entries(since, kind) if float(r["at"]) <= until]
            if not rows:
                last = self.last(kind)
                before = f"; the last one was {self._clock(float(last['at']))}" if last is not None else ""
                return ok(f"no {kind} logged {label}{before}")
            listed = ", ".join(f"{self._clock(float(r['at']))} {self.describe(r)}" for r in rows[-12:])
            return ok(f"{label}: {len(rows)} {kind} entr{'ies' if len(rows) != 1 else 'y'}: {listed}")
        words = self.day_words(self.day(since, until))
        asleep, row = self.sleep_now()
        if asleep and row is not None and label == "today so far":
            words += f"; asleep now, since {self._clock(float(row['at']))}"
        return ok(f"{label}: {words}")

    def _undo(self, kind: Any) -> str:
        kinds = (str(kind),) if isinstance(kind, str) and kind in KINDS else KINDS
        with self._lock:
            row = self._db().query_one(f"SELECT * FROM {TABLE} WHERE kind IN ({','.join('?' * len(kinds))}) "
                                       "ORDER BY added DESC, id DESC LIMIT 1", kinds)
            if row is None:
                return ok("nothing to take off")
            self.remove(int(row["id"]))
        log.info("baby log: the last %s taken off", row["kind"])
        return ok(f"took off {self.describe(row)} at {self._clock(float(row['at']))}")


__all__ = ["BabyLog", "BadTime", "KINDS", "ago_words", "past_time", "span_words"]
