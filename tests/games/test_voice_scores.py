"""Voice game scores in SQLite (littlebot/games/voice/scores.py): high scores, the spoken summary, recently asked
items, would-you-rather votes, and a broken database never raising."""

from __future__ import annotations

import time

from littlebot.games.voice.scores import KEEP_SEEN, ScoreStore, score_words, spoken_day
from voice_game_helpers import store


def test_best_scores_and_the_summary(ctx):
    s = store(ctx)
    assert s.best("trivia") is None and s.summary("trivia").startswith("No trivia scores yet.")
    assert s.summary() == "No scores yet. Want to play trivia?"
    s.record("trivia", score=45, out_of=5, best_streak=3, detail={"right": 4})
    s.clock["t"] += 100
    s.record("trivia", score=80, out_of=10, best_streak=5, detail={"right": 7, "topic": "science"})
    s.clock["t"] += 100
    s.record("trivia", score=80, out_of=10, best_streak=2, detail={"right": 8})  # a tie: the first one stays best
    s.record("hangman", score=4, out_of=5)
    best = s.best("trivia")
    assert best["score"] == 80 and best["detail"]["right"] == 7
    assert s.best("trivia", before=s.clock["t"] - 150)["score"] == 45
    assert s.best_streak("trivia") == 5 and s.played("trivia") == 3
    one = s.summary("trivia")
    assert one == "Your best trivia: 80 points, 7 of 10 right, today. Best streak: 5 in a row. You've played 3 games."
    all_ = s.summary()
    assert "Your best trivia: 80 points" in all_ and "Your best hangman: 4 of 5 words, today." in all_
    assert [r["score"] for r in s.recent("trivia", 2)] == [80, 80]


def test_words_and_days():
    assert score_words("riddles", {"score": 3, "out_of": 5, "detail": {}}) == "3 of 5 riddles"
    assert score_words("twenty_questions", {"score": 12, "detail": {"questions": 9}}) == "guessed it in 9 questions"
    now = time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1))
    assert spoken_day(now - 3600, now) == "today"
    assert spoken_day(now - 86400, now) == "yesterday"
    assert spoken_day(now - 3 * 86400, now).startswith("on ")
    assert spoken_day(now - 40 * 86400, now) == "on September 20"
    assert spoken_day("bad", now) == "a while ago"  # type: ignore[arg-type]


def test_recently_seen_items_are_kept_bounded(ctx):
    s = store(ctx)
    s.mark_seen("trivia", [(f"k{i}", f"question {i}") for i in range(KEEP_SEEN + 30)])
    seen = s.seen("trivia")
    assert len(seen) == KEEP_SEEN
    s.clock["t"] += 10
    s.mark_seen("trivia", [("k0", "question 0 again")])
    assert s.seen("trivia", 1) == [("k0", "question 0 again")]
    assert s.seen("riddles") == []


def test_votes(ctx):
    s = store(ctx)
    assert s.tally("fly-or-invisible") == [0, 0]
    s.vote("fly-or-invisible", 0)
    s.vote("fly-or-invisible", 0)
    assert s.vote("fly-or-invisible", 1) == [2, 1]
    assert s.tally("something-else") == [0, 0]


def test_a_broken_database_never_raises():
    class Broken:
        def executescript(self, *_a):
            raise RuntimeError("disk on fire")

    s = ScoreStore(Broken())
    assert not s.ok
    assert s.record("trivia", score=1) is None and s.best("trivia") is None and s.seen("trivia") == []
    assert s.vote("x", 0) == [0, 0] and s.summary() == "No scores yet. Want to play trivia?"
    assert ScoreStore(None).ok is False
