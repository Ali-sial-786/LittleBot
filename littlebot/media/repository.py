"""SQLite persistence for the media allowlist and daily watch-time usage (via ``ctx.db``)."""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from typing import Any

from littlebot.core.storage import Database
from littlebot.media.allowlist import (
    AllowlistFullError, DuplicateItemError, ParsedMedia, default_title, sanitize_title,
)

log = logging.getLogger("littlebot.media.repository")

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('video', 'playlist')),
    youtube_id TEXT,
    list_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    title_source TEXT NOT NULL DEFAULT 'parent',
    url TEXT NOT NULL,
    start_seconds INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    play_count INTEGER NOT NULL DEFAULT 0,
    last_played_at TEXT,
    last_error TEXT,
    last_error_at TEXT
);
CREATE INDEX IF NOT EXISTS media_items_order ON media_items (sort_order, id);
CREATE TABLE IF NOT EXISTS media_usage (
    day TEXT PRIMARY KEY,
    seconds REAL NOT NULL DEFAULT 0
);
"""

USAGE_RETENTION_DAYS = 60


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class AllowlistRepository:
    """CRUD for parent-approved items. All methods are thread-safe (Database holds a lock)."""

    def __init__(self, db: Database, *, max_items: int = 200) -> None:
        self.db = db
        self.max_items = max_items
        db.executescript(_SCHEMA)
        if db.kv_get("media:schema_version") != SCHEMA_VERSION:
            db.kv_set("media:schema_version", SCHEMA_VERSION)

    # -- reads ------------------------------------------------------------
    def list(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM media_items"
        if enabled_only:
            sql += " WHERE enabled = 1"
        return [self._row(r) for r in self.db.query(sql + " ORDER BY sort_order, id")]

    def get(self, item_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM media_items WHERE id = ?", (int(item_id),))
        return self._row(row) if row else None

    def find(self, dedupe_key: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM media_items WHERE dedupe_key = ?", (dedupe_key,))
        return self._row(row) if row else None

    def count(self, *, enabled_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM media_items" + (" WHERE enabled = 1" if enabled_only else "")
        row = self.db.query_one(sql)
        return int(row["n"]) if row else 0

    def next_enabled(self, after_id: int | None) -> dict[str, Any] | None:
        """The next enabled item in list order after ``after_id`` (wrapping); None if none enabled."""
        items = self.list(enabled_only=True)
        if not items:
            return None
        if after_id is None:
            return items[0]
        ordered = self.list()  # position of the current item even if it was disabled meanwhile
        ids = [i["id"] for i in ordered]
        if after_id not in ids:
            return items[0]
        pos = ids.index(after_id)
        rotated = ordered[pos + 1:] + ordered[: pos + 1]
        for item in rotated:
            if item["enabled"]:
                return item
        return None

    # -- writes -----------------------------------------------------------
    def add(self, parsed: ParsedMedia, title: str | None, *, title_source: str = "parent") -> dict[str, Any]:
        clean = sanitize_title(title)
        if not clean:
            clean, title_source = default_title(parsed), "default"
        existing = self.find(parsed.dedupe_key)
        if existing is not None:
            what = "playlist" if parsed.kind == "playlist" else "video"
            raise DuplicateItemError(f"That {what} is already on the approved list (“{existing['title']}”).",
                                     existing_id=existing["id"])
        if self.count() >= self.max_items:
            raise AllowlistFullError(f"The approved list is full ({self.max_items} items). Remove some first.")
        row = self.db.query_one("SELECT COALESCE(MAX(sort_order), 0) AS m FROM media_items")
        order = int(row["m"]) + 1 if row else 1
        try:
            cur = self.db.execute(
                "INSERT INTO media_items (kind, youtube_id, list_id, dedupe_key, title, title_source, url,"
                " start_seconds, added_at, enabled, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (parsed.kind, parsed.video_id, parsed.list_id, parsed.dedupe_key, clean, title_source,
                 parsed.canonical_url, int(parsed.start_seconds), _utc_now(), order),
            )
        except sqlite3.IntegrityError as e:  # concurrent duplicate add
            raise DuplicateItemError("That item is already on the approved list.") from e
        item = self.get(int(cur.lastrowid))
        assert item is not None
        return item

    def remove(self, item_id: int) -> bool:
        cur = self.db.execute("DELETE FROM media_items WHERE id = ?", (int(item_id),))
        return cur.rowcount > 0

    def update(self, item_id: int, *, title: str | None = None, enabled: bool | None = None,
               title_source: str = "parent") -> dict[str, Any] | None:
        if self.get(item_id) is None:
            return None
        if title is not None:
            clean = sanitize_title(title)
            if not clean:
                raise ValueError("The title can't be empty.")
            self.db.execute("UPDATE media_items SET title = ?, title_source = ? WHERE id = ?",
                            (clean, title_source, int(item_id)))
        if enabled is not None:
            self.db.execute("UPDATE media_items SET enabled = ? WHERE id = ?", (1 if enabled else 0, int(item_id)))
        return self.get(item_id)

    def set_title_if_default(self, item_id: int, title: str, *, source: str = "oembed") -> bool:
        clean = sanitize_title(title)
        if not clean:
            return False
        cur = self.db.execute(
            "UPDATE media_items SET title = ?, title_source = ? WHERE id = ? AND title_source = 'default'",
            (clean, source, int(item_id)),
        )
        return cur.rowcount > 0

    def reorder(self, ids: list[int]) -> list[dict[str, Any]]:
        """Put ``ids`` first in that order; items not listed keep their relative order after them."""
        current = [i["id"] for i in self.list()]
        wanted = [int(i) for i in ids if int(i) in current]
        rest = [i for i in current if i not in wanted]
        with self.db.transaction() as db:
            for pos, item_id in enumerate(wanted + rest, start=1):
                db.execute("UPDATE media_items SET sort_order = ? WHERE id = ?", (pos, item_id))
        return self.list()

    def record_play(self, item_id: int) -> None:
        self.db.execute("UPDATE media_items SET play_count = play_count + 1, last_played_at = ? WHERE id = ?",
                        (_utc_now(), int(item_id)))

    def set_error(self, item_id: int, message: str | None) -> None:
        self.db.execute("UPDATE media_items SET last_error = ?, last_error_at = ? WHERE id = ?",
                        (message, _utc_now() if message else None, int(item_id)))

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        out["enabled"] = bool(out.get("enabled"))
        out.pop("dedupe_key", None)
        vid = out.get("youtube_id")
        out["thumbnail_url"] = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else None
        return out


class UsageRepository:
    """Seconds of video watched per local calendar day (persisted so limits survive restarts)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        db.executescript(_SCHEMA)

    def get(self, day: _dt.date) -> float:
        row = self.db.query_one("SELECT seconds FROM media_usage WHERE day = ?", (day.isoformat(),))
        return float(row["seconds"]) if row else 0.0

    def add(self, day: _dt.date, seconds: float) -> float:
        if seconds <= 0:
            return self.get(day)
        self.db.execute(
            "INSERT INTO media_usage (day, seconds) VALUES (?, ?)"
            " ON CONFLICT(day) DO UPDATE SET seconds = seconds + excluded.seconds",
            (day.isoformat(), float(seconds)),
        )
        return self.get(day)

    def prune(self, today: _dt.date, keep_days: int = USAGE_RETENTION_DAYS) -> None:
        cutoff = (today - _dt.timedelta(days=keep_days)).isoformat()
        self.db.execute("DELETE FROM media_usage WHERE day < ?", (cutoff,))
