"""Things the family asked LittleBot to remember ("remember that Ali is Amara's dad").

A voice chat starts fresh every time, so what a grown-up asks it to remember is kept here: a few
short notes in the child profile (``profile.remembered``), on the robot only. Live voice reads them
at the start of every chat; the dashboard's Profile page lists them and can remove any of them.
"""

from __future__ import annotations

import re
from typing import Any

from littlebot.safety.content import find_blocked

MAX_NOTES = 20  # the oldest note goes when a new one would make more
MAX_LEN = 100


def clean_note(text: object) -> str | None:
    """One short line ("Ali is Amara's dad"), or None when there's nothing to keep."""
    note = " ".join(str(text or "").split()).strip(" \"'“”‘’").rstrip(".!,;: ")
    if not note or len(note) > MAX_LEN or find_blocked(note):
        return None
    return note[0].upper() + note[1:]


def notes(settings: Any) -> list[str]:
    prof = settings.get("profile") or {}
    raw = prof.get("remembered") if isinstance(prof, dict) else None
    return [n for n in (clean_note(x) for x in raw or []) if n]


def remember(settings: Any, text: object) -> str | None:
    """Keep ``text`` (moved to the end if it's already kept). The note as kept, or None if refused."""
    note = clean_note(text)
    if note is None:
        return None
    kept = [n for n in notes(settings) if n.lower() != note.lower()]
    kept = (kept + [note])[-MAX_NOTES:]
    _save(settings, kept)
    return note


def forget(settings: Any, text: object) -> list[str]:
    """Drop the notes that ``text`` is about ("Ali is the dad" forgets "Ali is Amara's dad"). The ones dropped."""
    want = _words(str(text or ""))
    if not want:
        return []
    kept, dropped = [], []
    for n in notes(settings):
        have = _words(n)
        (dropped if have and len(want & have) >= 0.6 * min(len(want), len(have)) else kept).append(n)
    if dropped:
        _save(settings, kept)
    return dropped


def _save(settings: Any, kept: list[str]) -> None:
    prof = dict(settings.get("profile") or {})
    prof["remembered"] = kept
    settings.set("profile", prof, source="voice")


_SMALL = {"a", "an", "the", "is", "are", "was", "and", "of", "to", "that", "s"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _SMALL}
