"""Voice game scores, recently asked items and would-you-rather votes, kept in the robot's SQLite (``ctx.db``).

Tables (created on first use; nothing here is about the child, no words of what was said are stored):

* ``voice_game_scores`` (game, score, out_of, best_streak, detail JSON, played_at): one row per finished game,
  at most :data:`KEEP_SCORES` per game. "What's my high score in trivia?" reads it.
* ``voice_game_seen`` (game, key, text, seen_at): what was asked lately (a question's hash and its words), so
  a new round doesn't repeat it; at most :data:`KEEP_SEEN` per game.
* ``voice_game_votes`` (item, choice, count): the would-you-rather tally ("most people here pick flying").

All methods are quick single statements (the database has its own lock) and never raise: a broken or missing
database just means no history.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger("littlebot.games.voice")

KEEP_SCORES = 500
KEEP_SEEN = 400

SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_game_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game TEXT NOT NULL,
    score INTEGER NOT NULL,
    out_of INTEGER NOT NULL DEFAULT 0,
    best_streak INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}',
    played_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS voice_game_scores_by_game ON voice_game_scores(game, score);
CREATE TABLE IF NOT EXISTS voice_game_seen (
    game TEXT NOT NULL,
    key TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    seen_at REAL NOT NULL,
    PRIMARY KEY (game, key)
);
CREATE TABLE IF NOT EXISTS voice_game_votes (
    item TEXT NOT NULL,
    choice INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item, choice)
);
"""

NAMES = {"trivia": "trivia", "hangman": "hangman", "twenty_questions": "twenty questions", "riddles": "riddles",
         "would_you_rather": "would you rather"}


class ScoreStore:
    def __init__(self, db: Any, *, now: Callable[[], float] = time.time) -> None:
        self.db = db
        self.now = now
        self.ok = False
        try:
            if db is not None:
                db.executescript(SCHEMA)
                self.ok = True
        except Exception:  # noqa: BLE001
            log.warning("voice games: the score tables could not be made", exc_info=True)

    # ------------------------------------------------------------ scores
    def record(self, game: str, *, score: int, out_of: int = 0, best_streak: int = 0,
               detail: Any = None) -> int | None:
        if not self.ok:
            return None
        try:
            data = detail if isinstance(detail, dict) else ({"topic": str(detail)} if detail else {})
            cur = self.db.execute(
                "INSERT INTO voice_game_scores(game, score, out_of, best_streak, detail, played_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (str(game)[:40], int(score), int(out_of), int(best_streak), json.dumps(data)[:500], float(self.now())))
            self.db.execute(
                "DELETE FROM voice_game_scores WHERE game=? AND id NOT IN (SELECT id FROM voice_game_scores "
                "WHERE game=? ORDER BY played_at DESC LIMIT ?)", (game, game, KEEP_SCORES))
            return int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001
            log.warning("voice games: saving a score failed", exc_info=True)
            return None

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        if not self.ok:
            return []
        try:
            rows = self.db.query(sql, params)
        except Exception:  # noqa: BLE001
            log.warning("voice games: reading scores failed", exc_info=True)
            return []
        for r in rows:
            try:
                r["detail"] = json.loads(r.get("detail") or "{}")
            except ValueError:
                r["detail"] = {}
        return rows

    def best(self, game: str, *, before: float | None = None) -> dict[str, Any] | None:
        """The highest score in ``game`` (the earliest of equal ones), optionally only games played before
        ``before``."""
        if before is None:
            rows = self._rows("SELECT * FROM voice_game_scores WHERE game=? ORDER BY score DESC, played_at ASC "
                              "LIMIT 1", (game,))
        else:
            rows = self._rows("SELECT * FROM voice_game_scores WHERE game=? AND played_at<? "
                              "ORDER BY score DESC, played_at ASC LIMIT 1", (game, float(before)))
        return rows[0] if rows else None

    def best_streak(self, game: str) -> int:
        rows = self._rows("SELECT MAX(best_streak) AS s, '{}' AS detail FROM voice_game_scores WHERE game=?", (game,))
        return int((rows[0].get("s") if rows else 0) or 0)

    def played(self, game: str) -> int:
        rows = self._rows("SELECT COUNT(*) AS n, '{}' AS detail FROM voice_game_scores WHERE game=?", (game,))
        return int((rows[0].get("n") if rows else 0) or 0)

    def recent(self, game: str, n: int = 5) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM voice_game_scores WHERE game=? ORDER BY played_at DESC LIMIT ?",
                          (game, max(1, int(n))))

    def summary(self, game: str | None = None) -> str:
        """A spoken line about the scores: one game's best, best streak and rounds, or every game's best."""
        games = [game] if game else list(NAMES)
        lines = []
        for g in games:
            if g == "would_you_rather":
                continue
            best = self.best(g)
            name = NAMES.get(g, g.replace("_", " "))
            if best is None:
                if game:
                    lines.append(f"No {name} scores yet. Say \"let's play {name}\" to set one!")
                continue
            line = f"Your best {name}: {score_words(g, best)}, {spoken_day(best['played_at'], self.now())}."
            if game:
                streak = self.best_streak(g)
                n = self.played(g)
                if streak >= 2:
                    line += f" Best streak: {streak} in a row."
                line += f" You've played {n} {'game' if n == 1 else 'games'}."
            lines.append(line)
        if not lines:
            return "No scores yet. Want to play trivia?"
        return " ".join(lines)

    # ------------------------------------------------------------ recently seen items
    def mark_seen(self, game: str, items: list[tuple[str, str]]) -> None:
        if not self.ok or not items:
            return
        now = float(self.now())
        try:
            self.db.executemany(
                "INSERT INTO voice_game_seen(game, key, text, seen_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(game, key) DO UPDATE SET seen_at=excluded.seen_at, text=excluded.text",
                [(game, str(k)[:64], str(t)[:160], now) for k, t in items])
            self.db.execute(
                "DELETE FROM voice_game_seen WHERE game=? AND key NOT IN (SELECT key FROM voice_game_seen "
                "WHERE game=? ORDER BY seen_at DESC LIMIT ?)", (game, game, KEEP_SEEN))
        except Exception:  # noqa: BLE001
            log.warning("voice games: remembering what was asked failed", exc_info=True)

    def seen(self, game: str, limit: int = KEEP_SEEN) -> list[tuple[str, str]]:
        """(key, text) of what was asked lately, newest first."""
        if not self.ok:
            return []
        try:
            rows = self.db.query("SELECT key, text FROM voice_game_seen WHERE game=? ORDER BY seen_at DESC LIMIT ?",
                                 (game, max(1, int(limit))))
        except Exception:  # noqa: BLE001
            return []
        return [(str(r["key"]), str(r["text"])) for r in rows]

    # ------------------------------------------------------------ would-you-rather votes
    def vote(self, item: str, choice: int) -> list[int]:
        if self.ok:
            try:
                self.db.execute("INSERT INTO voice_game_votes(item, choice, count) VALUES(?, ?, 1) "
                                "ON CONFLICT(item, choice) DO UPDATE SET count=count+1", (str(item)[:64], int(choice)))
            except Exception:  # noqa: BLE001
                log.warning("voice games: saving a vote failed", exc_info=True)
        return self.tally(item)

    def tally(self, item: str, choices: int = 2) -> list[int]:
        out = [0] * choices
        if not self.ok:
            return out
        try:
            rows = self.db.query("SELECT choice, count FROM voice_game_votes WHERE item=?", (str(item)[:64],))
        except Exception:  # noqa: BLE001
            return out
        for r in rows:
            c = int(r["choice"])
            if 0 <= c < choices:
                out[c] = int(r["count"])
        return out


def score_words(game: str, row: dict[str, Any]) -> str:
    score, out_of = int(row.get("score") or 0), int(row.get("out_of") or 0)
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    right = detail.get("right")
    if game == "trivia":
        extra = f", {right} of {out_of} right" if isinstance(right, int) and out_of else ""
        return f"{score} points{extra}"
    if game == "hangman":
        return f"{score} of {out_of} words" if out_of else f"{score} words"
    if game == "riddles":
        return f"{score} of {out_of} riddles" if out_of else f"{score} riddles"
    if game == "twenty_questions":
        q = detail.get("questions")
        return f"guessed it in {q} questions" if isinstance(q, int) and score else f"{score} points"
    return f"{score} of {out_of}" if out_of else str(score)


def spoken_day(ts: float, now: float) -> str:
    try:
        then, today = time.localtime(float(ts)), time.localtime(float(now))
    except (TypeError, ValueError, OverflowError):
        return "a while ago"
    days = (time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(then[:3] + (0, 0, 0, 0, 0, -1))) / 86400
    days = round(days)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "on " + time.strftime("%A", then)
    return "on " + time.strftime("%B", then) + f" {then.tm_mday}"


__all__ = ["KEEP_SCORES", "KEEP_SEEN", "NAMES", "ScoreStore", "score_words", "spoken_day"]
