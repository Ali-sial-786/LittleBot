"""The jokes LittleBot told lately, so "tell me a joke" gets one it hasn't told yet.

The brain only sees the last few minutes of a chat, so every new chat could tell the same joke again: "Why did the
robot go on a diet? Because it had too many bytes!" came in three chats in an hour (the robot, 2026-11-03), and
"remember to tell a different joke" only saved that wish. A turn that asks for a joke ("joke", "funny", "riddle",
"knock knock") gets a note with the last ones told; its answer is added to them.

Only LittleBot's own answers are kept, never what anyone said, in ``data/jokes_told.json`` (KEEP of them, each for
KEEP_DAYS), so a restart doesn't bring the old ones back.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("littlebot.assistant")

ASKED = re.compile(r"\b(?:jokes?|joking|funny|riddles?|puns?|knock[ -]knock|make me laugh)\b", re.IGNORECASE)
KEEP = 40
KEEP_DAYS = 30.0
NOTE_JOKES = 15  # the newest this many go in the note
JOKE_CHARS = 200


class JokesTold:
    def __init__(self, path: Path | None, *, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now
        self._items: list[tuple[float, str]] = []
        self._load()

    @classmethod
    def for_context(cls, ctx: Any) -> JokesTold:
        """data/jokes_told.json (``ctx.extras["jokes_told_file"]`` instead: the tests)."""
        given = (getattr(ctx, "extras", None) or {}).get("jokes_told_file")
        data = getattr(getattr(ctx, "config", None), "data_path", None)
        return cls(Path(given) if given else Path(data) / "jokes_told.json" if data else None)

    @staticmethod
    def asked(question: str) -> bool:
        return bool(ASKED.search(question or ""))

    def note(self, question: str) -> str:
        """A context note for a turn asking for a joke, or ""."""
        if not self.asked(question):
            return ""
        jokes = [j for _, j in self._fresh()[-NOTE_JOKES:]]
        if not jokes:
            return ""
        return ("jokes you already told this family lately (tell a different one, never one of these, unless they "
                "ask for one again): " + " | ".join(jokes))

    def told(self, question: str, answer: str) -> None:
        """After a turn: its answer is kept when the question asked for a joke."""
        answer = " ".join((answer or "").split())
        if not answer or not self.asked(question):
            return
        self._items = [*self._fresh(), (self._now(), answer[:JOKE_CHARS])][-KEEP:]
        self._save()

    def _fresh(self) -> list[tuple[float, str]]:
        cutoff = self._now() - KEEP_DAYS * 86400
        return [(at, j) for at, j in self._items if at >= cutoff]

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [(float(r["at"]), str(r["joke"])) for r in raw if isinstance(r, dict)][-KEEP:]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            log.info("assistant: the list of jokes told couldn't be read; starting a new one")

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([{"at": at, "joke": j} for at, j in self._items], ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.info("assistant: the list of jokes told couldn't be saved", exc_info=True)
