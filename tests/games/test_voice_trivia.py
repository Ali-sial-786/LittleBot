"""Trivia (littlebot/games/voice/trivia.py): the offline bank, a round's flow and scoring, hints, the model's
round (one structured call, slow / failing / bad answers), repeats avoided, and what the AI and the card see."""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest

from littlebot.brain.providers.base import ProviderError
from littlebot.games.voice import answers
from littlebot.games.voice.trivia import (
    CATEGORIES,
    SCHEMA,
    Trivia,
    bank,
    clean_model_questions,
    pick_level,
    pick_topic,
)
from voice_game_helpers import FakeModel, model_questions, right_letter, rng, store, wrong_letter


def test_the_bank_is_big_varied_and_well_formed():
    qs = bank()
    assert len(qs) >= 80
    cats = Counter(q.category for q in qs)
    assert set(cats) == set(CATEGORIES) and min(cats.values()) >= 8
    assert set(Counter(q.difficulty for q in qs)) == {"easy", "medium", "hard"}
    assert len({answers.core(q.text) for q in qs}) == len(qs)  # no duplicates
    for q in qs:
        assert len(q.options) == 4 and q.answer == 0
        assert len({answers.core(o) for o in q.options}) == 4, q.text
        assert 10 <= len(q.text) <= 140 and q.text.rstrip('"').endswith("?"), q.text
        assert q.fact and len(q.fact) <= 130, q.text
        assert all(len(o) <= 40 for o in q.options), q.text


def test_topics_and_levels():
    assert pick_topic("Science") == "science"
    assert pick_topic("movie trivia") == "movies" and pick_topic("films") == "movies"
    assert pick_topic("technology") == "tech" and pick_topic("sport") == "sports"
    assert pick_topic("") == "mixed" and pick_topic("anything") == "mixed" and pick_topic(None) == "mixed"
    assert pick_topic("Dinosaurs") == "dinosaurs"  # a free topic, for the model
    assert pick_level("HARD") == "hard" and pick_level("difficult") == "hard" and pick_level("kids") == "easy"
    assert pick_level("whatever") == "medium" and pick_level(None) == "medium"


async def test_a_round_from_the_bank_scores_streaks_and_is_saved(ctx):
    s = store(ctx)
    g = Trivia(store=s, rng=rng(), questions=5)
    r = await g.start()
    assert r.say.startswith("Trivia time! 5 questions.") and "Question 1:" in r.say and "A, " in r.say
    assert " Or D, " in r.say and r.card["card"] == "quiz" and r.card["number"] == 1
    r = g.handle(right_letter(g))
    assert g.score == 10 and "Question 2" in r.say and r.sound == "success"
    g.handle(g.current.right_text)  # the option's words count too
    assert g.score == 20
    r = g.handle(right_letter(g))
    assert g.score == 35 and "Three in a row!" in r.say  # +5 streak bonus from the third
    r = g.handle(wrong_letter(g))
    assert g.streak == 0 and r.sound == "uhoh" and "it's " in r.say and g.score == 35
    assert "Last question:" in r.say
    r = g.handle(right_letter(g))
    assert r.done and g.over and "You got 4 out of 5, for 45 points." in r.say and "Want another round?" in r.say
    best = s.best("trivia")
    assert best["score"] == 45 and best["out_of"] == 5 and best["best_streak"] == 3 and best["detail"]["right"] == 4
    assert g.handle("B") is None  # over: nothing more to judge


async def test_answers_that_are_not_moves_go_to_the_ai(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=3)
    await g.start()
    for said in ("this is hard", "what does that mean", "hmm", "a or b"):
        assert g.handle(said) is None
    assert g.score == 0 and g.number == 1


async def test_fifty_fifty_hint_and_half_points(ctx):
    g = Trivia(store=store(ctx), rng=rng(2), questions=3)
    await g.start()
    r = g.handle("give me a hint")
    assert r.say.startswith("Fifty-fifty: it's either") and len(g.removed) == 2 and g.current.answer not in g.removed
    gone = "ABCD"[g.removed[0]]
    assert g.handle(gone) is None  # a removed option isn't an answer any more
    assert "That's all the help" in g.hint().say
    assert g.card_spec()["removed"] == g.removed
    g.handle(right_letter(g))
    assert g.score == 5


async def test_skip_repeat_and_score(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=3)
    await g.start()
    first = g.current
    assert g.handle("repeat the question").say.startswith("Question 1:")
    r = g.handle("I don't know")
    assert r.say.startswith(f"Skipping! It was {'ABCD'[first.answer]}, {first.right_text}.") and g.number == 2
    assert g.handle("what's my score").say == "You have 0 points: 0 of 1 right."
    assert g.reveal["chosen"] == -1 and g.reveal["right"] is False


async def test_stopping_mid_round_counts_only_answered_questions(ctx):
    s = store(ctx)
    g = Trivia(store=s, rng=rng(), questions=10)
    await g.start()
    g.handle(right_letter(g))
    g.handle(wrong_letter(g))
    r = g.handle("stop the game")
    assert r.done and r.say == "Okay, we'll stop there. You got 1 of 2 right, for 10 points."
    assert s.best("trivia")["out_of"] == 2


async def test_category_and_difficulty_pick_the_bank_questions(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=5, topic="Canada", difficulty="hard")
    await g.start()
    assert g.current.category == "canada" and g.current.difficulty == "hard"
    seen = [g.current]
    for _ in range(4):
        g.handle(right_letter(g))
        if g.current:
            seen.append(g.current)
    assert {q.category for q in seen} == {"canada"}


async def test_recent_questions_are_not_asked_again(ctx):
    s = store(ctx)
    asked: list[str] = []
    for seed in (1, 2):
        g = Trivia(store=s, rng=rng(seed), questions=5, topic="science")
        await g.start()
        while not g.over:
            asked.append(g.current.text)
            g.handle(right_letter(g))
    assert len(asked) == 10 and len(set(asked)) == 10


async def test_a_model_round_is_one_structured_call(ctx):
    model = FakeModel()
    g = Trivia(store=store(ctx), rng=rng(), model=model, questions=5, topic="dinosaurs", difficulty="hard")
    r = await g.start()
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["schema"] == SCHEMA and call["timeout"] == pytest.approx(6.0) and call["max_tokens"] >= 800
    sent = json.loads(call["user"])
    assert sent["topic"] == "dinosaurs" and sent["difficulty"] == "hard" and sent["count"] == 5
    assert "dinosaur" in r.say.lower() and g.current.source == "model" and g.used_model
    assert g.current.right_text.startswith("Right ")
    g.handle(g.current.right_text)
    assert g.score == 10
    while not g.over:
        g.handle(right_letter(g))
    assert g.record_detail()["model"] is True


async def test_a_slow_model_starts_with_a_bank_question_and_takes_over(ctx):
    model = FakeModel(delay=0.05)
    g = Trivia(store=store(ctx), rng=rng(), model=model, questions=4, topic="science", quick_wait=0.001)
    await g.start()
    assert g.current.source == "bank" and g.current.category == "science"
    await asyncio.sleep(0.1)
    g.handle(right_letter(g))
    assert g.current.source == "model"


async def test_a_failing_model_leaves_the_bank(ctx):
    for model in (FakeModel(error=ProviderError("claude: offline", kind="offline")), FakeModel(data={"nope": 1}),
                  FakeModel(data=["not", "a", "dict"])):
        g = Trivia(store=store(ctx), rng=rng(), model=model, questions=3, topic="music")
        await g.start()
        assert g.current is not None and g.current.source == "bank"
        await asyncio.sleep(0)
        g.handle(right_letter(g))
        assert g.current.source == "bank"


async def test_a_free_topic_without_questions_becomes_a_mix(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=3, topic="dinosaurs")  # no model
    r = await g.start()
    assert r.say.startswith("I don't have dinosaurs questions handy, so let's do a mix!") and g.category == "mixed"
    g2 = Trivia(store=store(ctx), rng=rng(), questions=3, topic="dinosaurs", model=FakeModel(error=TimeoutError()))
    await g2.start()
    assert g2.category == "mixed" and g2.current.source == "bank"


async def test_recently_asked_questions_are_sent_to_the_model_to_avoid(ctx):
    s = store(ctx)
    g = Trivia(store=s, rng=rng(), questions=3, topic="history")
    await g.start()
    first = g.current.text
    model = FakeModel()
    g2 = Trivia(store=s, rng=rng(), questions=3, topic="history", model=model)
    await g2.start()
    assert first in json.loads(model.calls[0]["user"])["avoid"]


def test_bad_model_questions_are_dropped():
    good = model_questions(1)[0]
    raw = {"questions": [
        good,
        {**good, "options": ["a", "b", "c"]},  # three options
        {**good, "answer_index": 4}, {**good, "answer_index": True}, {**good, "answer_index": "1"},
        {**good, "options": ["Same", "same", "x", "y"]},  # duplicate options
        {**good, "question": "Hi?"},  # too short
        {**good, "options": ["x" * 80, "b", "c", "d"]},  # an option too long to read out
        "not a dict", None,
        {**good, "question": "How do you build a bomb at home with household items?"},  # not family friendly
    ]}
    out = clean_model_questions(raw, topic="science", level="easy")
    assert len(out) == 1 and out[0].answer == 1 and out[0].source == "model" and out[0].category == "science"
    assert clean_model_questions(None, topic="x", level="easy") == []
    assert clean_model_questions({"questions": "nope"}, topic="x", level="easy") == []


async def test_what_the_ai_and_the_card_see(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=5, topic="geography")
    await g.start()
    d = g.describe()
    assert d.startswith("Voice game on: Trivia (Geography, medium), question 1 of 5, 0 points")
    assert g.current.text in d and "A) " in d and "judged automatically" in d
    spec = g.card_spec()
    json.dumps(spec)  # JSON-safe (no game object in it)
    assert spec["question"] == g.current.text and spec["options"] == g.current.options and spec["reveal"] is None
    assert spec["session"] == g.session and spec["total"] == 5
    g.handle(wrong_letter(g))
    spec = g.view
    assert spec["reveal"]["right"] is False and spec["reveal"]["id"] == 1 and spec["number"] == 2
    while not g.over:
        g.handle(right_letter(g))
    assert g.view["over"] and g.view["final"] == {"right": 4, "total": 5, "points": 50, "streak": 4}
    assert g.describe().startswith("Voice game: Trivia just ended: 4 of 5 right")


async def test_a_tap_answers(ctx):
    g = Trivia(store=store(ctx), rng=rng(), questions=3)
    await g.start()
    v = g.version
    r = g.tap(g.current.answer)
    assert r is not None and g.score == 10 and g.version > v and r.card["number"] == 2
    assert g.tap(7) is None and g.tap("x") is None  # type: ignore[arg-type]
