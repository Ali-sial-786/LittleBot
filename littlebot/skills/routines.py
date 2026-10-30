"""Routines made by voice: "when I say good night, turn off the lights, set an alarm for 7 and tell me tomorrow's
weather".

``create_routine(name, trigger_phrase, steps)`` keeps the steps as plain-language instructions (SQLite, on the
robot); running one (``run_routine``) hands them back to the brain to carry out now, in order, with its own tools
(smart home, alarms, the weather ...), so a routine can use anything LittleBot can do, today's skills and
tomorrow's. The context note tells the brain each routine's trigger phrase, and :meth:`Routines.match_phrase`
lets the assistant start one locally when a whole utterance is just the phrase ("good night", "good night
LittleBot").

Steps never bypass a confirmation: the run instruction says that anything needing the user's yes (unlocking a
door) still asks first.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from typing import Any, Callable

from littlebot.skills.base import STR, Skill, ToolSpec, as_json, err, ok, tool

log = logging.getLogger("littlebot.skills.routines")

MAX_ROUTINES = 30
MAX_STEPS = 12
MAX_STEP_CHARS = 200
MAX_NAME_CHARS = 60
NOTE_ROUTINES = 15  # trigger phrases in the context note
NEAR = 0.88  # how close a heard phrase must be to a trigger phrase (difflib ratio) to count
_SCHEMA = """
CREATE TABLE IF NOT EXISTS routines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    trigger_phrase TEXT NOT NULL,
    trigger_key TEXT NOT NULL,
    steps TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_run_at REAL,
    run_count INTEGER NOT NULL DEFAULT 0
);
"""
# Words around a trigger phrase that don't change it ("hey LittleBot, good night please").
_AROUND = frozenset("hey hi ok okay please littlebot little bot robot now then thanks thank you so um uh well".split())
_RUN = re.compile(r"^(?:(?:please|can you|could you|let's|lets)\s+)*(?:run|start|do|begin|play|go)\s+(?:the\s+|my\s+|our\s+)?"
                  r"(?P<name>.+?)(?:\s+routine)?$")


def key(text: Any) -> str:
    """Lower case, letters and digits only, single spaces ("Good Night!" -> "good night")."""
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'", "").replace("’", "")))


def trimmed(text: Any) -> str:
    """``key`` without the polite words around it ("hey littlebot good night please" -> "good night")."""
    w = key(text).split()
    while w and w[0] in _AROUND:
        w.pop(0)
    while w and w[-1] in _AROUND:
        w.pop()
    return " ".join(w)


def _clean(text: Any, limit: int) -> str:
    s = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).split()).strip(" .,;")
    return s[:limit].strip()


class Routines(Skill):
    name = "routines"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time) -> None:
        super().__init__(ctx, now=now)
        self._ready = False
        self._phrases: list[tuple[str, str, str]] = []  # (name, trigger phrase, trigger key): the note, match_phrase
        try:
            self._setup()
        except Exception:  # noqa: BLE001 - a broken database: no routines, the rest carries on
            log.exception("routines: the database couldn't be set up")

    def _setup(self) -> None:
        self.ctx.db.executescript(_SCHEMA)
        self._ready = True
        self._reload()

    def _reload(self) -> None:
        rows = self.ctx.db.query("SELECT name, trigger_phrase, trigger_key FROM routines ORDER BY id")
        self._phrases = [(r["name"], r["trigger_phrase"], r["trigger_key"]) for r in rows]

    # ------------------------------------------------------------ the skill
    def available(self) -> bool:
        return self._ready and bool(getattr(self.ctx.config.skills, "routines", True))

    def tools(self) -> list[ToolSpec]:
        return [
            tool("create_routine", "Save a routine the user describes ('when I say good night, turn off the lights "
                 "and set an alarm for 7'): a short name, the phrase that starts it, and the steps as plain "
                 "instructions, each one thing to do. Saving a name again replaces that routine.",
                 {"name": {**STR, "description": "a short name, e.g. 'good night'"},
                  "trigger_phrase": {**STR, "description": "what the user will say to start it, e.g. 'good night'"},
                  "steps": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_STEPS,
                            "description": "plain instructions in order, e.g. ['turn off the living room lights', "
                                           "'set an alarm for 7 am', 'tell me tomorrow's weather']"}},
                 ("name", "steps")),
            tool("list_routines", "The routines the user has made, with what starts them and their steps."),
            tool("delete_routine", "Delete one of the user's routines by name.",
                 {"name": {**STR, "description": "the routine's name"}}, ("name",)),
            tool("run_routine", "Run one of the user's routines now (by its name or its trigger phrase): returns the "
                 "steps to carry out with your tools.", {"name": {**STR, "description": "name or trigger phrase"}},
                 ("name",)),
        ]

    def prompt_note(self) -> str:
        return ("Routines: the user can make routines by voice ('when I say good night, turn off the lights and set "
                "an alarm for 7'). Read the steps back in one short sentence, then create_routine (name, "
                "trigger_phrase, steps as plain instructions). When the user says a routine's phrase, call "
                "run_routine and carry out its steps with your tools, then confirm in one short sentence.")

    def context_note(self) -> str:
        if not self._phrases:
            return ""
        parts = [f"'{phrase}' -> run_routine('{name}')" for name, phrase, _ in self._phrases[:NOTE_ROUTINES]]
        more = f" (+{len(self._phrases) - NOTE_ROUTINES} more: list_routines)" if len(self._phrases) > NOTE_ROUTINES \
            else ""
        return "Routines: when the user says " + "; ".join(parts) + more + "."

    def status(self) -> dict[str, Any]:
        return {**super().status(), "routines": len(self._phrases)}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        if not self._ready:
            return err("routines aren't available (the database couldn't be opened)")
        if name == "create_routine":
            return self._create(args)
        if name == "list_routines":
            return self._list()
        if name == "delete_routine":
            return self._delete(args)
        if name == "run_routine":
            return self._run(args)
        return await super().run(name, args)

    # ------------------------------------------------------------ for the assistant
    def match_phrase(self, text: str) -> str | None:
        """The routine a whole utterance asks for ("good night", "hey LittleBot, good night", "run my bedtime
        routine"), else None. Exact after trimming polite words, or very close for longer phrases."""
        if not self.available():
            return None
        heard = trimmed(text)
        if not heard:
            return None
        for name, _, tkey in self._phrases:
            if heard == tkey or heard == key(name):
                return name
        m = _RUN.match(heard)
        if m:
            wanted = trimmed(m.group("name"))
            for name, _, tkey in self._phrases:
                if wanted in (key(name), tkey):
                    return name
        best, score = None, 0.0
        for name, _, tkey in self._phrases:
            if len(tkey) < 6 or abs(len(tkey) - len(heard)) > 4:
                continue
            r = difflib.SequenceMatcher(None, heard, tkey).ratio()
            if r > score:
                best, score = name, r
        return best if score >= NEAR else None

    # ------------------------------------------------------------ the tools
    def _find(self, wanted: str) -> dict[str, Any] | None:
        k = trimmed(wanted)
        if not k:
            return None
        sql = "SELECT * FROM routines WHERE name_key = ? OR trigger_key = ? ORDER BY name_key = ? DESC, id LIMIT 1"
        row = self.ctx.db.query_one(sql, (k, k, k))
        if row is None:
            k2 = re.sub(r"\s+routine$", "", re.sub(r"^(?:the|my|our)\s+", "", k))
            row = self.ctx.db.query_one(sql, (k2, k2, k2))
        if row is None:
            keys = {r["name_key"]: r for r in self.ctx.db.query("SELECT * FROM routines")}
            close = difflib.get_close_matches(k, list(keys), n=1, cutoff=0.8)
            row = keys[close[0]] if close else None
        return row

    def _create(self, args: dict[str, Any]) -> str:
        name = _clean(args.get("name"), MAX_NAME_CHARS)
        phrase = _clean(args.get("trigger_phrase"), 80) or name
        raw = args.get("steps")
        if isinstance(raw, str):
            raw = [s for s in re.split(r"\s*(?:\n|;|\bthen\b)\s*", raw) if s.strip()]
        if not name or not key(name):
            return err("give the routine a short name")
        if not isinstance(raw, list) or not raw:
            return err("give the routine's steps, each one thing to do")
        steps = [_clean(s, MAX_STEP_CHARS) for s in raw if isinstance(s, (str, int, float))]
        steps = [s for s in steps if s]
        if not steps:
            return err("give the routine's steps, each one thing to do")
        if len(steps) > MAX_STEPS:
            return err(f"a routine can have at most {MAX_STEPS} steps")
        name_key, trigger_key = key(name), trimmed(phrase) or key(phrase)
        if not trigger_key:
            return err("give the phrase that starts the routine")
        clash = self.ctx.db.query_one(
            "SELECT name FROM routines WHERE name_key != ? AND (trigger_key IN (?, ?) OR name_key = ?)",
            (name_key, trigger_key, name_key, trigger_key))
        if clash:
            return err(f"'{phrase}' already starts the routine '{clash['name']}'; pick another phrase, or replace "
                       f"that routine by saving it under its name")
        existing = self.ctx.db.query_one("SELECT id FROM routines WHERE name_key = ?", (name_key,))
        now = float(self.now())
        if existing is None:
            count = (self.ctx.db.query_one("SELECT COUNT(*) AS n FROM routines") or {}).get("n", 0)
            if count >= MAX_ROUTINES:
                return err(f"there are already {MAX_ROUTINES} routines; delete one first")
            self.ctx.db.execute(
                "INSERT INTO routines (name, name_key, trigger_phrase, trigger_key, steps, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (name, name_key, phrase, trigger_key, json.dumps(steps), now, now))
        else:
            self.ctx.db.execute(
                "UPDATE routines SET name = ?, trigger_phrase = ?, trigger_key = ?, steps = ?, updated_at = ? "
                "WHERE id = ?", (name, phrase, trigger_key, json.dumps(steps), now, existing["id"]))
        self._reload()
        log.info("routines: %s a routine with %d steps", "saved" if existing is None else "replaced", len(steps))
        verb = "saved" if existing is None else "updated"
        return ok(f"{verb} the routine '{name}' ({len(steps)} step{'s' if len(steps) != 1 else ''}); it runs when "
                  f"the user says '{phrase}'")

    def _list(self) -> str:
        rows = self.ctx.db.query("SELECT * FROM routines ORDER BY id")
        if not rows:
            return ok("no routines yet; the user can make one, e.g. 'when I say good night, turn off the lights'")
        return as_json([{"name": r["name"], "say": r["trigger_phrase"], "steps": _steps(r)} for r in rows])

    def _delete(self, args: dict[str, Any]) -> str:
        wanted = _clean(args.get("name"), 80)
        if not wanted:
            return err("say which routine")
        row = self._find(wanted)
        if row is None:
            return err(f"there is no routine called '{wanted}'" + self._known())
        self.ctx.db.execute("DELETE FROM routines WHERE id = ?", (row["id"],))
        self._reload()
        log.info("routines: deleted one")
        return ok(f"deleted the routine '{row['name']}'")

    def _run(self, args: dict[str, Any]) -> str:
        wanted = _clean(args.get("name"), 80)
        if not wanted:
            return err("say which routine")
        row = self._find(wanted)
        if row is None:
            name = self.match_phrase(wanted)
            row = self._find(name) if name else None
        if row is None:
            return err(f"there is no routine called '{wanted}'" + self._known())
        steps = _steps(row)
        if not steps:
            return err(f"the routine '{row['name']}' has no steps")
        self.ctx.db.execute("UPDATE routines SET last_run_at = ?, run_count = run_count + 1 WHERE id = ?",
                            (float(self.now()), row["id"]))
        log.info("routines: running one (%d steps)", len(steps))
        listed = " ".join(f"{i}. {s.rstrip('.')}." for i, s in enumerate(steps, 1))
        return ok(f"routine '{row['name']}': run these now, in order, with your tools, then confirm briefly in one "
                  f"sentence (anything that needs the user's yes, like unlocking, still asks first): {listed}")

    def _known(self) -> str:
        names = [n for n, _, _ in self._phrases][:8]
        return f"; the routines are: {', '.join(names)}" if names else "; there are no routines yet"


def _steps(row: dict[str, Any]) -> list[str]:
    try:
        steps = json.loads(row.get("steps") or "[]")
    except ValueError:
        return []
    return [str(s) for s in steps if isinstance(s, str) and s.strip()] if isinstance(steps, list) else []


__all__ = ["Routines", "key", "trimmed"]
