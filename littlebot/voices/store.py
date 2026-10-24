"""Voiceprints of the people on Who's who, in LittleBot's database (never a recording).

The people themselves are the face recognition's (``littlebot.vision.faces.PeopleStore``, table
``face_people``): one list of the family, with a name and a role each. This adds ``voice_samples``: per
sample, the voiceprint's numbers (192 float32 for the default model, 768 bytes), which model made them,
whether it came from the dashboard's recording or was learned in a chat, and when. The table only accepts
rows of at most 2 KB, far too small to hold sound. Deleting a person deletes their voiceprints too
(``ON DELETE CASCADE``); deleted rows are overwritten on disk (``secure_delete``, set by PeopleStore).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import threading
from typing import Any

import numpy as np

from littlebot.vision.faces import PeopleStore
from littlebot.voices.engine import unit

RECORDED = "dashboard"  # a sample from the dashboard's "Record voice"
LEARNED = "chat"  # a sample learned during a voice chat
MAX_RECORDED = 8  # per person: two recordings; the oldest goes first
MAX_LEARNED = 8  # per person, learned in chats; the oldest goes first (recordings are never pushed out by these)

_SCHEMA = """
-- a voiceprint per sample (at most 512 float32 numbers), never a recording
CREATE TABLE IF NOT EXISTS voice_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES face_people(id) ON DELETE CASCADE,
    features BLOB NOT NULL CHECK (length(features) BETWEEN 256 AND 2048 AND length(features) % 4 = 0),
    model TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('dashboard', 'chat')),
    taken_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_samples_person ON voice_samples (person_id, id);
"""


@dataclasses.dataclass(frozen=True, eq=False)
class Voice:
    """Someone LittleBot may recognise by voice: the average of their voiceprints (unit length, never shown or sent)."""

    id: int
    name: str
    role: str
    centre: np.ndarray = dataclasses.field(repr=False)
    samples: int = 1

    @property
    def label(self) -> str:
        return f"{self.name} ({self.role})" if self.role else self.name


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


class VoiceStore:
    """Voiceprints per person (thread-safe: the DB locks). ``model``: the model file's name; samples another
    model made are ignored (they don't compare) until deleted."""

    def __init__(self, db: Any, model: str) -> None:
        self.db = db
        self.people = PeopleStore(db)  # the family list (and secure_delete)
        db.executescript(_SCHEMA)
        self.model = model
        self._lock = threading.Lock()
        self._version = 0
        self._cache: tuple[tuple[int, int, int], dict[int, tuple[np.ndarray, int]]] | None = None

    # -- reads
    def counts(self) -> dict[int, dict[str, Any]]:
        """Per person: how many voiceprints (recorded / learned) and the newest's date. Never the numbers."""
        rows = self.db.query("SELECT person_id, source, COUNT(*) AS n, MAX(taken_at) AS last FROM voice_samples "
                             "WHERE model = ? GROUP BY person_id, source", (self.model,))
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            c = out.setdefault(int(r["person_id"]), {"samples": 0, "recorded": 0, "learned": 0, "last_at": None})
            c["samples"] += int(r["n"])
            c["recorded" if r["source"] == RECORDED else "learned"] += int(r["n"])
            c["last_at"] = max(filter(None, (c["last_at"], r["last"])), default=None)
        return out

    def count(self, person_id: int) -> int:
        return int(self.counts().get(int(person_id), {}).get("samples", 0))

    def known(self) -> list[Voice]:
        """Everyone with at least one voiceprint, with their average (the averages are cached until a change)."""
        row = self.db.query_one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM voice_samples WHERE model = ?",
                                (self.model,))
        key = (self._version, int(row["n"]), int(row["m"])) if row else (self._version, 0, 0)
        cached = self._cache
        if cached is not None and cached[0] == key:
            centres = cached[1]
        else:
            grouped: dict[int, list[np.ndarray]] = {}
            for r in self.db.query("SELECT person_id, features FROM voice_samples WHERE model = ? ORDER BY id",
                                   (self.model,)):
                try:
                    grouped.setdefault(int(r["person_id"]), []).append(unit(np.frombuffer(r["features"], dtype="<f4")))
                except ValueError:
                    continue
            centres = {pid: (unit(np.mean(vs, axis=0)), len(vs)) for pid, vs in sorted(grouped.items())
                       if len({v.size for v in vs}) == 1}
            self._cache = (key, centres)
        names = {int(r["id"]): r for r in self.db.query("SELECT id, name, role FROM face_people")}
        return [Voice(pid, names[pid]["name"], names[pid]["role"] or "", centre, n)
                for pid, (centre, n) in centres.items() if pid in names]

    def has_samples(self) -> bool:
        return self.db.query_one("SELECT 1 AS x FROM voice_samples WHERE model = ? LIMIT 1", (self.model,)) is not None

    # -- writes
    def add(self, person_id: int, features: Any, source: str) -> int:
        """Keep one voiceprint for ``person_id``; how many they have now. Past the limit for its kind
        (MAX_RECORDED / MAX_LEARNED) the oldest of that kind goes."""
        if self.people.person(person_id) is None:
            raise KeyError("That person isn't on the list.")
        if source not in (RECORDED, LEARNED):
            raise ValueError("unknown voiceprint source")
        blob = unit(features).astype("<f4").tobytes()
        limit = MAX_RECORDED if source == RECORDED else MAX_LEARNED
        with self._lock, self.db.transaction() as db:
            db.execute("INSERT INTO voice_samples (person_id, features, model, source, taken_at) VALUES (?, ?, ?, ?, ?)",
                       (int(person_id), blob, self.model, source, _utc_now()))
            db.execute("DELETE FROM voice_samples WHERE person_id = ? AND source = ? AND id NOT IN (SELECT id FROM "
                       "voice_samples WHERE person_id = ? AND source = ? ORDER BY id DESC LIMIT ?)",
                       (int(person_id), source, int(person_id), source, limit))
            self._version += 1
        return self.count(person_id)

    def forget(self, person_id: int) -> int:
        """Delete every voiceprint of ``person_id`` (any model); how many."""
        with self._lock:
            cur = self.db.execute("DELETE FROM voice_samples WHERE person_id = ?", (int(person_id),))
            self._version += 1
        self.people._flush()  # noqa: SLF001 - the same database file: push overwritten pages out of the WAL
        return cur.rowcount

    def forget_all(self) -> int:
        with self._lock:
            cur = self.db.execute("DELETE FROM voice_samples")
            self._version += 1
        self.people._flush()  # noqa: SLF001
        return cur.rowcount


__all__ = ["LEARNED", "MAX_LEARNED", "MAX_RECORDED", "RECORDED", "Voice", "VoiceStore"]
