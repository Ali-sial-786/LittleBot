"""Messages for the family: "tell Amara her lunch is in the fridge", "let Dad know I'll be late", "any messages for
me?", "did Amara get my message?".

A message waits in SQLite on the robot (``skill_messages``) until it is given. The brain's context lists the ones
waiting (for whom, from whom, how long ago); when the person talking is recognised (littlebot/assistant/who.py) or
says who they are, and a message waits for them, the brain gives it once and calls ``message_given``. A given
message is kept a day (for "what did it say again?" and "did she get it?"), one never given ``KEEP_DAYS`` days.
Nothing leaves the robot. The names waiting for a message are words speech-to-text listens harder for.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

from littlebot.skills.base import Skill, ToolSpec, as_json, err, ok, tool

log = logging.getLogger("littlebot.skills.messages")

TABLE = "skill_messages"
KEEP_DAYS = 14
GIVEN_KEEP_S = 86400.0
MAX_WAITING = 30
MAX_TEXT = 300
MAX_NAME = 40
NOTE_MESSAGES = 6
NOTE_TEXT = 140
_LEAD = re.compile(r"^(?:(?:my|our|the|to|for|dear)\s+)+", re.I)


def name_key(value: Any) -> str:
    """ "Amara", "amara" and "AMARA." are one person; "my dad" is "dad"."""
    if not isinstance(value, str):
        return ""
    s = " ".join(re.sub(r"[^\w\s'-]", " ", value.lower()).split())
    return _LEAD.sub("", s).strip(" '-")[:MAX_NAME]


def _name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    s = " ".join(value.split()).strip(" \"'.,;:!?")
    s = _LEAD.sub("", s)
    return s[:1].upper() + s[1:MAX_NAME] if s else ""


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    s = " ".join(value.split()).strip(" \"'")
    return s[:MAX_TEXT].rsplit(" ", 1)[0] + " ..." if len(s) > MAX_TEXT else s


def _ago(seconds: float) -> str:
    m = int(max(0, seconds) // 60)
    if m < 2:
        return "just now"
    if m < 60:
        return f"{m} minutes ago"
    h = m // 60
    if h < 24:
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = h // 24
    return f"{d} day{'s' if d != 1 else ''} ago"


class Messages(Skill):
    name = "messages"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._ready = False
        self._lock = threading.RLock()

    def available(self) -> bool:
        return bool(getattr(getattr(self.ctx.config, "skills", None), "messages", True))

    # ------------------------------------------------------------ storage
    def _db(self) -> Any:
        db = self.ctx.db
        if not self._ready:
            db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                       "to_name TEXT NOT NULL, to_key TEXT NOT NULL, from_name TEXT NOT NULL DEFAULT '', "
                       "text TEXT NOT NULL, left_at REAL NOT NULL, given_at REAL)")
            self._ready = True
        return db

    def _tidy(self) -> None:
        now = self.now()
        self._db().execute(f"DELETE FROM {TABLE} WHERE (given_at IS NOT NULL AND given_at<?) OR left_at<?",
                           (now - GIVEN_KEEP_S, now - KEEP_DAYS * 86400))

    def waiting(self, to: str | None = None) -> list[dict[str, Any]]:
        """The messages not given yet (for ``to`` only, when given), oldest first."""
        with self._lock:
            self._tidy()
            if to:
                return self._db().query(f"SELECT * FROM {TABLE} WHERE given_at IS NULL AND to_key=? ORDER BY left_at, "
                                        "id", (name_key(to),))
            return self._db().query(f"SELECT * FROM {TABLE} WHERE given_at IS NULL ORDER BY left_at, id")

    def given(self, to: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            self._tidy()
            if to:
                return self._db().query(f"SELECT * FROM {TABLE} WHERE given_at IS NOT NULL AND to_key=? ORDER BY "
                                        "given_at", (name_key(to),))
            return self._db().query(f"SELECT * FROM {TABLE} WHERE given_at IS NOT NULL ORDER BY given_at")

    def leave(self, to: str, text: str, sender: str = "") -> int:
        with self._lock:
            self._db().execute(f"INSERT INTO {TABLE}(to_name, to_key, from_name, text, left_at) VALUES(?, ?, ?, ?, ?)",
                               (_name(to), name_key(to), _name(sender), text, self.now()))
            row = self._db().query_one(f"SELECT MAX(id) AS id FROM {TABLE}")
        return int(row["id"]) if row else 0

    def mark_given(self, ids: list[int]) -> list[dict[str, Any]]:
        out = []
        with self._lock:
            for i in ids:
                row = self._db().query_one(f"SELECT * FROM {TABLE} WHERE id=? AND given_at IS NULL", (int(i),))
                if row is not None:
                    self._db().execute(f"UPDATE {TABLE} SET given_at=? WHERE id=?", (self.now(), int(i)))
                    out.append(row)
        return out

    def _row(self, r: dict[str, Any]) -> dict[str, Any]:
        out = {"id": int(r["id"]), "for": r["to_name"], "text": r["text"], "left": _ago(self.now() - float(r["left_at"]))}
        if r.get("from_name"):
            out["from"] = r["from_name"]
        if r.get("given_at") is not None:
            out["given"] = _ago(self.now() - float(r["given_at"]))
        return out

    # ------------------------------------------------------------ for the model
    def tools(self) -> list[ToolSpec]:
        who = {"type": "string", "description": "The person's name or what the family calls them ('Amara', 'Dad')."}
        return [
            tool("leave_message", "Keep a message for a family member who isn't here now, to give them the next "
                 "time they talk to you ('tell Amara her lunch is in the fridge').", {
                     "to": who,
                     "text": {"type": "string", "description": "What to tell them, as you'll say it ('her lunch is "
                              "in the fridge')."},
                     "sender": {"type": "string", "description": "Who it is from, when you know who is talking."}},
                 ("to", "text")),
            tool("messages_for", "The messages waiting for someone (and the ones given in the last day), or for "
                 "everyone when no one is named.", {"to": who}),
            tool("message_given", "Mark messages as given, right after you told the person them.",
                 {"ids": {"type": "array", "items": {"type": "integer"}}}, ("ids",)),
            tool("cancel_message", "Take back a message not given yet (when the one who left it asks).",
                 {"id": {"type": "integer"}}, ("id",)),
        ]

    def prompt_note(self) -> str:
        return ("Messages: 'tell Amara ...', 'let Dad know ...' for someone not here now: leave_message (to, the words "
                "to pass on, sender if you know who is talking); say you'll tell them. Waiting messages are in the "
                "context. When the person talking is recognised, or says who they are, and one waits for them, give "
                "it once near the start of your answer ('Ali left you a message: ...'), then message_given. Give a "
                "message only to the person it is for; to anyone else say only whether it was given.")

    def context_note(self) -> str:
        if not self.available():
            return ""
        try:
            rows = self.waiting()
        except Exception:  # noqa: BLE001
            return ""
        if not rows:
            return ""
        now = self.now()
        items = []
        for r in rows[:NOTE_MESSAGES]:
            text = str(r["text"])
            text = text if len(text) <= NOTE_TEXT else text[:NOTE_TEXT].rsplit(" ", 1)[0] + " ..."
            sender = f" from {r['from_name']}" if r.get("from_name") else ""
            items.append(f"#{r['id']} for {r['to_name']}{sender}, {_ago(now - float(r['left_at']))}: \"{text}\"")
        more = f" (and {len(rows) - NOTE_MESSAGES} more: messages_for)" if len(rows) > NOTE_MESSAGES else ""
        return "Messages waiting (give each only to that person): " + "; ".join(items) + more + "."

    def hearing_words(self) -> list[str]:
        try:
            return list(dict.fromkeys(str(r["to_name"]) for r in self.waiting() if r.get("to_name")))[:10]
        except Exception:  # noqa: BLE001
            return []

    def status(self) -> dict[str, Any]:
        try:
            n = len(self.waiting())
        except Exception:  # noqa: BLE001
            n = 0
        return {**super().status(), "waiting": n}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if not self.available():
            return err("messages are switched off")
        if name == "leave_message":
            to, text = _name(args.get("to")), _text(args.get("text"))
            if not name_key(args.get("to")):
                return err("say who the message is for")
            if not text:
                return err("give the message's words in text")
            if len(self.waiting()) >= MAX_WAITING:
                return err(f"{MAX_WAITING} messages are already waiting; give or cancel some first")
            mid = self.leave(to, text, str(args.get("sender") or ""))
            log.info("messages: one left (#%d)", mid)
            return ok(f"message #{mid} kept for {to}; I'll give it the next time {to} talks to me")
        if name == "messages_for":
            to = args.get("to") if isinstance(args.get("to"), str) and name_key(args.get("to")) else None
            waiting = [self._row(r) for r in self.waiting(to)]
            given = [self._row(r) for r in self.given(to)]
            if not waiting and not given:
                return ok(f"no messages for {_name(to)}" if to else "no messages waiting")
            return as_json({"waiting": waiting, "given_today": given})
        if name == "message_given":
            raw = args.get("ids")
            ids = [int(i) for i in (raw if isinstance(raw, list) else [raw])
                   if isinstance(i, (int, float)) and not isinstance(i, bool)]
            if not ids:
                return err("give the ids of the messages you told them")
            done = self.mark_given(ids)
            if not done:
                return ok("those were already given (or don't exist)")
            log.info("messages: %d given", len(done))
            return ok(f"marked {len(done)} message{'s' if len(done) != 1 else ''} as given")
        if name == "cancel_message":
            mid = args.get("id")
            if not isinstance(mid, (int, float)) or isinstance(mid, bool):
                return err("give the message's id")
            with self._lock:
                row = self._db().query_one(f"SELECT * FROM {TABLE} WHERE id=? AND given_at IS NULL", (int(mid),))
                if row is None:
                    return err(f"no message #{int(mid)} waiting")
                self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (int(mid),))
            return ok(f"message #{int(mid)} for {row['to_name']} taken back")
        return err(f"messages has no tool called {str(name)[:40]}")


__all__ = ["Messages", "name_key"]
