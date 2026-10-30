"""Reminders and alarms: "remind me at 6 to call mom", "wake me at 7 on weekdays", "snooze", "stop".

Kept in SQLite (``skill_reminders``) so they survive restarts. A background loop (:meth:`Reminders.start`) calls
:meth:`Reminders.tick` every second; ``tick`` is plain code on ``self.now`` (wall-clock seconds), so tests drive
it with their own clock.

When one is due:

* a **reminder** chimes once and is said out loud (``announce(..., urgent=True)`` on ``assistant.announce``,
  whichever voice is on speaks it), with the reminders card ringing (Snooze / Stop) for REMINDER_RING_S;
* an **alarm** rings: the bell every BELL_EVERY_S (``ctx.audio.play_sound("bell", wait=False)``) until it is
  stopped, snoozed or has rung ``skills.alarm_ring_seconds``; it is announced and the card rings too.

Both publish ``reminders.ringing`` {id, kind, label} and, when they end, ``reminders.stopped`` {id, kind, label,
reason: "stopped" | "snoozed" | "timeout" | "cancelled", until?}. A repeating one (daily, weekdays, weekly, chosen
weekdays) moves on to its next time as soon as it rings; a one-off is deleted once it has rung.

Missed while LittleBot was off (or its clock jumped): up to RING_LATE_S late still rings; up to MISSED_MAX_S late
is announced once ("I missed a reminder while I was off: ..."); later than that it is dropped with a log line.

Times are the home's (``info.timezone``, else the robot's own zone): see littlebot/skills/when.py.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import difflib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from littlebot.skills import when as W
from littlebot.skills.base import INT, STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.reminders")

TABLE = "skill_reminders"
RINGING = "reminders.ringing"  # {id, kind, label}
STOPPED = "reminders.stopped"  # {id, kind, label, reason, until?}
TICK_S = 1.0
RECHECK_S = 60.0  # the database is looked at at least this often (else only when something is due)
RING_LATE_S = 300.0  # this late it still rings (the loop was busy, the clock was just set)
MISSED_MAX_S = 2 * 3600.0  # later than this (LittleBot was off): dropped quietly
REMINDER_RING_S = 45.0  # a reminder's card rings this long (an alarm: skills.alarm_ring_seconds)
BELL_EVERY_S = 2.5  # an alarm's bell (the sound is 1.6 s)
FIRST_BELL_GAP_S = 6.0  # after the first bell: quiet while the announcement is said
SNOOZE_AGAIN_S = 600.0  # "snooze" soon after something rang (it already stopped) still snoozes it
MAX_PENDING = 100
MAX_TEXT = 160
MAX_LABEL = 60
MAX_SNOOZE = 120
SHOW_LINGER_S = 12.0  # the card after setting / cancelling (a short look)

_SMALL = {"a", "an", "the", "my", "to", "for", "at", "on", "in", "of", "and", "me", "remind", "reminder",
          "reminders", "alarm", "alarms", "cancel", "delete", "remove", "about", "that", "this", "one", "set", "is",
          "it", "please", "am", "pm", "today", "tomorrow", "morning", "evening", "tonight", "o", "clock"}
_ALL = re.compile(r"^(?:all|everything|every one|all of them|both)(?:\s+(?:my|the|of\s+my|of\s+the))?"
                  r"(?:\s+(?:reminders?|alarms?))?$")


@dataclass
class Item:
    id: int
    kind: str  # reminder | alarm
    text: str  # what to say ("call mom"); an alarm's label ("" for none)
    due: float  # next time it is due (wall-clock seconds); a repeating one: its next time
    hour: int
    minute: int
    days: frozenset[int] = frozenset()  # repeats on these weekdays (0 = Monday); empty: once
    snooze_until: float | None = None
    state: str = "pending"  # pending | ringing
    rang_at: float | None = None
    created: float = 0.0

    @property
    def fire_at(self) -> float:
        return self.snooze_until if self.snooze_until is not None else self.due

    @property
    def label(self) -> str:
        return self.text or ("alarm" if self.kind == "alarm" else "reminder")

    @property
    def repeats(self) -> bool:
        return bool(self.days)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "text": self.text, "label": self.label, "at": self.fire_at,
                "repeat": W.repeat_words(self.days), "state": self.state, "snoozed": self.snooze_until is not None}


@dataclass
class _Ring:
    item: Item
    started: float
    next_bell: float
    limit: float


@dataclass
class _Recent:
    item: Item
    at: float  # when it stopped


def clean_text(value: Any, limit: int = MAX_TEXT) -> str:
    """One tidy line from the model (no quotes around it, no "remind me to")."""
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    s = " ".join(str(value).split()).strip(" \"'“”‘’.!,;:")
    s = re.sub(r"^(?:remind\s+me\s+(?:to\s+|that\s+|about\s+)?|reminder\s*(?:to|:)\s*|to\s+)", "", s, flags=re.I)
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return s


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _SMALL}


class Reminders(Skill):
    name = "reminders"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._lock = threading.RLock()
        self._ready = False
        self._rings: dict[int, _Ring] = {}
        self._recent: _Recent | None = None
        self._next_check = 0.0
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[Any] = set()
        self._tz: _dt.tzinfo | None | bool = False  # False: not looked up yet
        self.version = 0  # bumped on every change (the card redraws)

    # ------------------------------------------------------------ settings
    def _cfg(self) -> Any:
        return getattr(self.ctx.config, "skills", None)

    def available(self) -> bool:
        return bool(getattr(self._cfg(), "reminders", True))

    def snooze_minutes(self) -> int:
        return int(getattr(self._cfg(), "reminder_snooze_minutes", 9) or 9)

    def alarm_seconds(self) -> float:
        return float(getattr(self._cfg(), "alarm_ring_seconds", 60) or 60)

    def zone(self) -> _dt.tzinfo | None:
        """The home's time zone (``info.timezone``, else the system's by name); None: the robot's local time."""
        if self._tz is False:
            from littlebot.info.clock import local_zone_name  # noqa: PLC0415

            name = str(getattr(getattr(self.ctx.config, "info", None), "timezone", "") or "").strip()
            tz: _dt.tzinfo | None = None
            for candidate in (name, local_zone_name() or ""):
                if not candidate:
                    continue
                try:
                    tz = ZoneInfo(candidate)
                    break
                except (ZoneInfoNotFoundError, ValueError):
                    log.warning("reminders: unknown time zone %r", candidate[:60])
            self._tz = tz
        return self._tz if self._tz is not False else None  # type: ignore[return-value]

    def local(self, ts: float | None = None) -> _dt.datetime:
        return W.now_local(self.now() if ts is None else ts, self.zone())

    # ------------------------------------------------------------ storage
    def _db(self) -> Any:
        db = self.ctx.db
        if not self._ready:
            db.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE} ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', "
                "due REAL NOT NULL, hour INTEGER NOT NULL, minute INTEGER NOT NULL, days TEXT NOT NULL DEFAULT '', "
                "snooze_until REAL, state TEXT NOT NULL DEFAULT 'pending', rang_at REAL, created REAL NOT NULL)")
            self._ready = True
        return db

    @staticmethod
    def _item(row: dict[str, Any]) -> Item:
        return Item(id=int(row["id"]), kind=str(row["kind"]), text=str(row["text"] or ""), due=float(row["due"]),
                    hour=int(row["hour"]), minute=int(row["minute"]), days=W.days_from_text(row["days"]),
                    snooze_until=None if row["snooze_until"] is None else float(row["snooze_until"]),
                    state=str(row["state"]), rang_at=None if row["rang_at"] is None else float(row["rang_at"]),
                    created=float(row["created"] or 0))

    def _items(self, where: str = "", params: tuple[Any, ...] = ()) -> list[Item]:
        rows = self._db().query(f"SELECT * FROM {TABLE} {where} ORDER BY COALESCE(snooze_until, due), id", params)
        return [self._item(r) for r in rows]

    def item(self, rid: int) -> Item | None:
        with self._lock:
            got = self._items("WHERE id=?", (int(rid),))
        return got[0] if got else None

    def upcoming(self, limit: int | None = None, kind: str | None = None) -> list[Item]:
        """Pending ones, soonest first."""
        with self._lock:
            items = self._items("WHERE state='pending'" + (" AND kind=?" if kind else ""), (kind,) if kind else ())
        return items[:limit] if limit else items

    def ringing(self) -> list[Item]:
        with self._lock:
            return [r.item for r in sorted(self._rings.values(), key=lambda r: r.started)]

    def is_ringing(self, rid: int) -> bool:
        with self._lock:
            return int(rid) in self._rings

    def _changed(self) -> None:
        self.version += 1
        self._next_check = 0.0  # look again on the next tick

    def add(self, kind: str, text: str, at: W.When) -> Item:
        now = self.now()
        cur = self._db().execute(
            f"INSERT INTO {TABLE}(kind, text, due, hour, minute, days, created) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (kind, text, at.at.timestamp(), at.hour, at.minute, W.days_text(at.days), now))
        self._changed()
        found = self.item(int(cur.lastrowid or 0))
        assert found is not None
        return found

    def _next_after(self, item: Item, after: float) -> float:
        return W.next_time(W.now_local(after, self.zone()), item.hour, item.minute, item.days, self.zone()).timestamp()

    def _advance(self, item: Item, now: float) -> float:
        """A repeating one's next time: its ``due`` while that is still to come (it already moved on when it first
        rang; a snooze rang since), else the next one after ``now``."""
        return item.due if item.due > now else self._next_after(item, now)

    def _done(self, item: Item, now: float) -> None:
        """It has rung (or was missed): a repeating one waits for its next time, a one-off is gone."""
        if item.repeats:
            nxt = self._advance(item, now)
            self._db().execute(f"UPDATE {TABLE} SET due=?, snooze_until=NULL, state='pending' WHERE id=?",
                               (nxt, item.id))
        else:
            self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (item.id,))
        self._changed()

    # ------------------------------------------------------------ the clock
    def tick(self) -> None:
        """Ring what is due, keep alarms ringing, end the ones that rang long enough. Never raises."""
        try:
            self._tick()
        except Exception:  # noqa: BLE001 - the loop carries on
            log.exception("reminders: tick failed")

    def _tick(self) -> None:
        now = self.now()
        with self._lock:
            for ring in list(self._rings.values()):
                if now - ring.started >= ring.limit:
                    self._stop([ring], now, "timeout")
                elif ring.item.kind == "alarm" and now >= ring.next_bell:
                    ring.next_bell = now + BELL_EVERY_S
                    self._sound("bell")
            if now < self._next_check:
                return
            due = self._items("WHERE state='pending' AND COALESCE(snooze_until, due) <= ?", (now,))
            missed: list[Item] = []
            for item in due:
                late = now - item.fire_at
                if late <= RING_LATE_S:
                    self._ring(item, now)
                    continue
                if late > MISSED_MAX_S:
                    log.info("reminders: dropped %s %s that was due %d minutes ago (LittleBot was off)",
                             "an" if item.kind == "alarm" else "a", item.kind, late // 60)
                else:
                    missed.append(item)
                if not item.repeats:
                    self._done(item, now)
                    continue
                # A repeating one: on to its next time, counting one that came round a moment ago (it still rings).
                nxt = self._next_after(item, now - RING_LATE_S)
                self._db().execute(f"UPDATE {TABLE} SET due=?, snooze_until=NULL, state='pending' WHERE id=?",
                                   (nxt, item.id))
                self._changed()
                if nxt <= now:
                    item.due, item.snooze_until = nxt, None
                    self._ring(item, now)
            nxt = self._db().query_one(
                f"SELECT MIN(COALESCE(snooze_until, due)) AS t FROM {TABLE} WHERE state='pending'")
            soonest = float(nxt["t"]) if nxt and nxt["t"] is not None else now + RECHECK_S
            self._next_check = min(soonest, now + RECHECK_S)
        if missed:
            self._tell_missed(missed)

    def catch_up(self) -> None:
        """On start: what was ringing when LittleBot stopped is let go; what was missed is handled by ``tick``."""
        now = self.now()
        with self._lock:
            for item in self._items("WHERE state='ringing'"):
                log.info("reminders: a %s was ringing when LittleBot stopped; let go", item.kind)
                self._done(item, now)
        self.tick()

    def _ring(self, item: Item, now: float) -> None:
        nxt = self._advance(item, now) if item.repeats else item.due
        self._db().execute(f"UPDATE {TABLE} SET state='ringing', rang_at=?, snooze_until=NULL, due=? WHERE id=?",
                           (now, nxt, item.id))
        item.state, item.rang_at, item.snooze_until, item.due = "ringing", now, None, nxt
        limit = self.alarm_seconds() if item.kind == "alarm" else REMINDER_RING_S
        self._rings[item.id] = _Ring(item, now, now + FIRST_BELL_GAP_S, limit)
        self._changed()
        log.info("reminders: %s %d is ringing", item.kind, item.id)
        self._publish(RINGING, {"id": item.id, "kind": item.kind, "label": item.label})
        self._sound("bell" if item.kind == "alarm" else "chime")
        spec = {"card": "reminders", "ringing": item.id}
        self.announce(self._ring_words(item, now), kind=item.kind, card=spec, urgent=True)
        self._spawn(self.show(spec))

    def _ring_words(self, item: Item, now: float) -> str:
        clock = W.clock_words(self.local(now))
        if item.kind == "reminder":
            return f"Reminder: {item.text}." if item.text else f"It's {clock}: here's your reminder."
        if item.text:
            return f"It's {clock}: {item.text}!"
        if 4 <= self.local(now).hour < 11:
            return f"Good morning! It's {clock}, time to get up."
        return f"It's {clock}: your alarm."

    def _tell_missed(self, missed: list[Item]) -> None:
        parts = []
        for it in missed[:3]:
            when = W.clock_words(self.local(it.fire_at))
            parts.append(f"{it.text} at {when}" if it.kind == "reminder" and it.text else
                         f"your {it.label if it.kind == 'alarm' and it.text else it.kind} at {when}")
        more = f", and {len(missed) - 3} more" if len(missed) > 3 else ""
        noun = "reminder" if len(missed) == 1 else "reminders"
        if all(it.kind == "alarm" for it in missed):
            noun = "alarm" if len(missed) == 1 else "alarms"
        text = f"Sorry, I missed {'a' if len(missed) == 1 else len(missed)} {noun} while I was off: " + \
            "; ".join(parts) + more + "."
        log.info("reminders: %d missed while off, announced", len(missed))
        self.announce(text, kind="reminder", urgent=False)

    # ------------------------------------------------------------ stop / snooze (tools and the card)
    def stop_ringing(self, rid: int | None = None, reason: str = "stopped") -> list[Item]:
        """Stop what is ringing (``rid``: only that one). The ones stopped."""
        with self._lock:
            rings = [r for r in self._rings.values() if rid is None or r.item.id == int(rid)]
            return self._stop(rings, self.now(), reason)

    def _stop(self, rings: list[_Ring], now: float, reason: str) -> list[Item]:
        out = []
        for ring in rings:
            self._rings.pop(ring.item.id, None)
            current = self.item(ring.item.id) or ring.item
            self._done(current, now)
            self._recent = _Recent(current, now)
            out.append(current)
            log.info("reminders: %s %d %s", current.kind, current.id, reason)
            self._publish(STOPPED, {"id": current.id, "kind": current.kind, "label": current.label, "reason": reason})
        return out

    def snooze(self, minutes: int | None = None, rid: int | None = None) -> list[Item]:
        """Snooze what is ringing (or what just rang) for ``minutes``. The ones snoozed (``fire_at``: when next)."""
        mins = max(1, min(MAX_SNOOZE, int(minutes or self.snooze_minutes())))
        with self._lock:
            now = self.now()
            until = now + mins * 60
            rings = [r for r in self._rings.values() if rid is None or r.item.id == int(rid)]
            out: list[Item] = []
            for ring in rings:
                self._rings.pop(ring.item.id, None)
                self._db().execute(f"UPDATE {TABLE} SET state='pending', snooze_until=? WHERE id=?",
                                   (until, ring.item.id))
                self._changed()
                out.append(self.item(ring.item.id) or ring.item)
            if not rings and self._recent is not None and now - self._recent.at <= SNOOZE_AGAIN_S and (
                    rid is None or self._recent.item.id == int(rid)):
                it = self._recent.item
                if self.item(it.id) is not None:  # a repeating one: still there, waiting for its next time
                    self._db().execute(f"UPDATE {TABLE} SET snooze_until=? WHERE id=?", (until, it.id))
                    self._changed()
                    out.append(self.item(it.id) or it)
                else:  # a one-off that rang and went: back, for a while
                    out.append(self.add(it.kind, it.text, W.When(self.local(until))))
                self._recent = None
            for it in out:
                log.info("reminders: %s %d snoozed for %d minutes", it.kind, it.id, mins)
                self._publish(STOPPED, {"id": it.id, "kind": it.kind, "label": it.label, "reason": "snoozed",
                                        "until": until})
            return out

    def cancel(self, items: list[Item]) -> None:
        with self._lock:
            for it in items:
                if it.id in self._rings:
                    self._rings.pop(it.id, None)
                    self._publish(STOPPED, {"id": it.id, "kind": it.kind, "label": it.label, "reason": "cancelled"})
                self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (it.id,))
            self._changed()

    # ------------------------------------------------------------ background
    async def start(self) -> None:
        if not self.available():
            return
        self.catch_up()
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="reminders")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(TICK_S)

    async def drain(self) -> None:
        """Wait for the sounds and cards it started (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        runtime = getattr(self.ctx, "runtime", None)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        try:
            if loop is None and runtime is not None and getattr(runtime, "running", False):
                runtime.submit(coro)
                return
            if loop is None:
                coro.close()  # nowhere to run it (a tap with no runtime)
                return
            task = loop.create_task(coro)
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:  # noqa: BLE001
            coro.close()
            log.debug("reminders: couldn't start a task", exc_info=True)

    def _sound(self, name: str) -> None:
        audio = getattr(self.ctx, "audio", None)
        if audio is None:
            return
        try:
            self._spawn(audio.play_sound(name, wait=False))
        except Exception:  # noqa: BLE001
            log.debug("reminders: no sound", exc_info=True)

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        try:
            self.ctx.bus.publish(topic, data, source=self.name)
        except Exception:  # noqa: BLE001
            log.debug("reminders: publish failed", exc_info=True)

    # ------------------------------------------------------------ for the model
    def tools(self) -> list[ToolSpec]:
        repeat = {"type": "string", "description": "Optional: 'daily', 'weekdays', 'weekends', 'weekly', or days "
                                                   "like 'monday,wednesday'. Leave out for once."}
        when_help = ("An ISO-8601 local date-time ('2026-10-30T18:00'), or the user's words: 'in 20 minutes', "
                     "'at 6pm', 'tomorrow at 7:30', 'Friday at 5pm', 'every weekday at 7'.")
        return [
            tool("set_reminder", "Set a reminder: LittleBot says it out loud at that time (and shows it). The "
                 "result has the exact time: confirm it briefly.",
                 {"text": {"type": "string", "description": "What to remind them of, short, as it should be said: "
                                                            "'call mom', 'take the bins out'."},
                  "when": {"type": "string", "description": when_help}, "repeat": repeat},
                 ("text", "when")),
            tool("set_alarm", "Set an alarm: it rings with a bell until stopped or snoozed. 'Wake me up at 7', "
                 "'an alarm for 6:30 on weekdays'. The result has the exact time: confirm it briefly.",
                 {"time": {"type": "string", "description": when_help},
                  "label": {"type": "string", "description": "Optional: what it is for ('gym', 'school run')."},
                  "repeat": repeat},
                 ("time",)),
            tool("list_reminders", "The reminders and alarms coming up (shown on the screen too).",
                 {"kind": {"type": "string", "enum": ["all", "reminder", "alarm"]}}),
            tool("cancel_reminder", "Cancel a reminder or alarm, by its id (from list_reminders or when it was set) "
                 "or by words ('call mom', '7am alarm', 'all alarms').",
                 {"id": INT, "text": STR}),
            tool("snooze", "Snooze the alarm or reminder that is ringing (or just rang).",
                 {"minutes": {"type": "integer", "description": "Optional, default "
                                                                f"{self.snooze_minutes()}."}}),
            tool("stop_alarm", "Stop the alarm or reminder that is ringing."),
        ]

    def prompt_note(self) -> str:
        return ("Reminders and alarms: set_reminder says something at a time; set_alarm rings until stopped. Pass "
                "the time as an ISO-8601 local time or the user's own words, then confirm the exact time from the "
                "result in a few words. 'Stop' or 'snooze' while one rings: stop_alarm / snooze. Times are local.")

    def context_note(self) -> str:
        if not self.available():
            return ""
        now = self.now()
        lines = []
        for it in self.ringing():
            noun = "An alarm" if it.kind == "alarm" else "A reminder"
            lines.append(f"{noun} is ringing now ({it.label}): stop_alarm or snooze stops it.")
        nxt = self.upcoming(3)
        if nxt:
            here = self.local(now)
            parts = []
            for it in nxt:
                at = self.local(it.fire_at)
                what = (f'alarm "{it.text}"' if it.text else "alarm") if it.kind == "alarm" else it.text
                rep = f", {W.repeat_words(it.days)}" if it.days else ""
                snz = ", snoozed" if it.snooze_until is not None else ""
                parts.append(f"{W.clock_words(at)} {W.day_words(at, here)}: {what} (id {it.id}{rep}{snz})")
            lines.append("Coming up: " + "; ".join(parts) + ".")
        return "\n".join(lines)

    def status(self) -> dict[str, Any]:
        return {**super().status(), "pending": len(self.upcoming()), "ringing": [i.id for i in self.ringing()]}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if not self.available():
            return err("reminders are switched off")
        if name == "set_reminder":
            return await self._set("reminder", args.get("text"), args.get("when"), args.get("repeat"))
        if name == "set_alarm":
            return await self._set("alarm", args.get("label"), args.get("time") or args.get("when"),
                                   args.get("repeat"))
        if name == "list_reminders":
            return await self._list(str(args.get("kind") or "all"))
        if name == "cancel_reminder":
            return await self._cancel(args.get("id"), args.get("text"))
        if name == "snooze":
            return self._snooze_tool(args.get("minutes"))
        if name == "stop_alarm":
            stopped = self.stop_ringing()
            if not stopped:
                return ok("nothing is ringing")
            return ok("stopped " + " and ".join(self._name(i) for i in stopped))
        return err(f"reminders has no tool called {str(name)[:40]}")

    def _name(self, it: Item) -> str:
        if it.kind == "alarm":
            return f"the {it.text} alarm" if it.text else "the alarm"
        return f"the reminder to {it.text}" if it.text else "the reminder"

    def _describe(self, it: Item, now: float) -> str:
        at = self.local(it.fire_at)
        rep = f", {W.repeat_words(it.days)}" if it.days else ""
        what = f' "{it.text}"' if it.text else ""
        snz = " (snoozed)" if it.snooze_until is not None else ""
        return f"{it.kind} {it.id}{what} at {W.say_when(at, self.local(now))}{rep}{snz}"

    async def _set(self, kind: str, text_arg: Any, when_arg: Any, repeat_arg: Any) -> str:
        text = clean_text(text_arg, MAX_TEXT if kind == "reminder" else MAX_LABEL)
        if kind == "reminder" and not text:
            return err("say what the reminder is for (text)")
        if kind == "alarm":
            text = re.sub(r"\s*alarm$", "", text, flags=re.I).strip()
        if isinstance(when_arg, (int, float)) and not isinstance(when_arg, bool):
            when_arg = str(when_arg)
        if not isinstance(when_arg, str) or not when_arg.strip():
            return err(f"give the {'time' if kind == 'alarm' else 'when'}: {W.HINT}")
        try:
            repeat = W.parse_repeat(repeat_arg) if repeat_arg not in (None, "") else None
            now_dt = self.local()
            got = W.parse_when(when_arg, now_dt, tz=self.zone(), alarm=kind == "alarm", repeat=repeat)
        except W.WhenError as e:
            return err(str(e))
        now = self.now()
        with self._lock:
            pending = self.upcoming()
            if len(pending) >= MAX_PENDING:
                return err(f"there are already {MAX_PENDING} reminders and alarms; cancel some first")
            same = next((it for it in pending if it.kind == kind and abs(it.fire_at - got.at.timestamp()) < 60
                         and it.text.lower() == text.lower() and it.days == got.days), None)
            if same is not None:
                return ok(f"that's already set: {self._describe(same, now)}")
            item = self.add(kind, text, got)
        log.info("reminders: %s %d set (%s)", kind, item.id, "repeats" if item.days else "once")
        await self._show_list(item.id)
        return ok(f"{self._describe(item, now)}, {W.until_words(item.fire_at - now)}")

    async def _show_list(self, highlight: int | None = None) -> None:
        if self.ringing():
            return  # the ringing card stays
        spec: dict[str, Any] = {"card": "reminders", "linger": SHOW_LINGER_S}
        if highlight is not None:
            spec["highlight"] = highlight
        await self.show(spec)

    async def _list(self, kind: str) -> str:
        kind = kind.lower().rstrip("s")
        kind_f = kind if kind in ("reminder", "alarm") else None
        items = self.upcoming(kind=kind_f)
        now = self.now()
        ring = [f"ringing now: {self._name(i)}" for i in self.ringing()]
        await self.show({"card": "reminders"})
        if not items:
            what = {"reminder": "reminders", "alarm": "alarms"}.get(kind_f or "", "reminders or alarms")
            return ok("; ".join(ring + [f"no {what} set"]))
        shown = items[:10]
        more = f"; and {len(items) - 10} more" if len(items) > 10 else ""
        return ok(f"{len(items)} coming up: " + "; ".join(ring + [self._describe(i, now) for i in shown]) + more)

    def match(self, text: str) -> list[Item]:
        """The pending / ringing ones ``text`` names ("call mom", "the 7am alarm", "all alarms")."""
        q = " ".join(str(text or "").lower().split()).strip(" .!?")
        kind = "alarm" if re.search(r"\balarms?\b", q) else "reminder" if re.search(r"\breminders?\b", q) else None
        with self._lock:
            pool = self._items("WHERE state IN ('pending', 'ringing')" + (" AND kind=?" if kind else ""),
                               (kind,) if kind else ())
        q = re.sub(r"^(?:cancel|delete|remove|turn off|stop)\s+", "", q)
        if _ALL.match(q) or q in ("alarms", "reminders", "all"):
            return pool
        clock = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?(?=\s|$)", q)
        if clock and (clock.group(2) or clock.group(3) or re.search(r"\b(?:at|for)\s+\d", q) or kind):
            h, m = int(clock.group(1)), int(clock.group(2) or 0)
            ampm = (clock.group(3) or "").replace(".", "")
            hours = {h % 12 + (12 if ampm == "pm" else 0)} if ampm else {h % 24, (h % 12) + 12}
            pool = [it for it in pool if self.local(it.fire_at).hour in hours and self.local(it.fire_at).minute == m]
            q = (q[:clock.start()] + " " + q[clock.end():]).strip()
        want = _words(q)
        if not want:
            return pool if clock or kind else []
        scored = []
        for it in pool:
            have = _words(it.text)
            hit = len(want & have)
            fuzzy = sum(1 for w in want - have if difflib.get_close_matches(w, list(have), n=1, cutoff=0.8))
            score = (hit + 0.8 * fuzzy) / len(want)
            if score >= 0.5:
                scored.append((score, it))
        if not scored:
            return []
        best = max(s for s, _ in scored)
        return [it for s, it in scored if s >= best - 1e-9]

    async def _cancel(self, rid: Any, text: Any) -> str:
        now = self.now()
        items: list[Item] = []
        if rid not in (None, ""):
            try:
                it = self.item(int(rid))
            except (TypeError, ValueError):
                return err("id must be a number")
            if it is None:
                return err(f"there is no reminder or alarm {rid}")
            items = [it]
        elif isinstance(text, str) and text.strip():
            items = self.match(text)
            if not items:
                left = self.upcoming(5)
                there = "; ".join(self._describe(i, now) for i in left) or "none are set"
                return err(f"nothing matches '{text.strip()[:40]}'. Set now: {there}")
            all_asked = bool(_ALL.match(re.sub(r"^(?:cancel|delete|remove)\s+", "", text.lower().strip())))
            if len(items) > 1 and not all_asked:
                return ok("several match: " + "; ".join(self._describe(i, now) for i in items[:6]) +
                          ". Ask which one, then cancel it by id (or say all of them).")
        else:
            return err("say which one: its id or its words")
        self.cancel(items)
        await self._show_list()
        if len(items) == 1:
            return ok(f"cancelled {self._describe(items[0], now)}")
        return ok(f"cancelled {len(items)}: " + "; ".join(self._describe(i, now) for i in items[:6]))

    def _snooze_tool(self, minutes: Any) -> str:
        if minutes in (None, ""):
            mins = self.snooze_minutes()
        else:
            try:
                mins = int(float(minutes))
            except (TypeError, ValueError):
                return err("minutes must be a number")
            if not 1 <= mins <= MAX_SNOOZE:
                return err(f"snooze for 1 to {MAX_SNOOZE} minutes")
        done = self.snooze(mins)
        if not done:
            return ok("nothing is ringing to snooze")
        until = W.clock_words(self.local(done[0].fire_at))
        return ok(f"snoozed {' and '.join(self._name(i) for i in done)} for {mins} minutes, until {until}")


__all__ = ["RINGING", "STOPPED", "Item", "Reminders", "clean_text"]
