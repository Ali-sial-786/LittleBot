"""Twenty Questions (littlebot/games/voice/twenty_questions.py): guesses judged locally, questions counted and
left to the AI (which sees the secret), the GPT-Live tool path, the limit and the final guess."""

from __future__ import annotations

import json

from littlebot.games.voice.twenty_questions import TwentyQuestions, pick_category, things
from voice_game_helpers import rng, store


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _secret(g, name):
    g.secret = next(t for t in things() if t["name"] == name)


async def _game(ctx, name="a penguin", **kw):
    clock = kw.pop("clock", Clock())
    g = TwentyQuestions(store=store(ctx), rng=rng(), clock=clock, **kw)
    await g.start()
    _secret(g, name)
    return g


def test_the_things():
    ts = things()
    assert len(ts) >= 100 and len({t["name"] for t in ts}) == len(ts)
    assert {t["category"] for t in ts} == {"animal", "food", "object", "vehicle", "nature"}
    assert all(t["hint"] and t["name"].split()[-1].lower() not in t["hint"].lower() for t in ts), \
        [t["name"] for t in ts if t["name"].split()[-1].lower() in t["hint"].lower()]
    assert pick_category("animals") == "animal" and pick_category("") == ""


async def test_the_start_and_a_category(ctx):
    g = TwentyQuestions(store=store(ctx), rng=rng(), topic="animals")
    r = await g.start()
    assert r.say.startswith("Twenty Questions! I'm thinking of an animal.") and g.secret["category"] == "animal"
    hard = TwentyQuestions(store=store(ctx), rng=rng(), difficulty="hard")
    assert "I'm thinking of something." in (await hard.start()).say


async def test_questions_are_counted_and_left_to_the_ai(ctx):
    clock = Clock()
    g = await _game(ctx, clock=clock)
    assert g.handle("does it live in water?") is None and g.questions == 1
    d = g.describe()
    assert "thinking of: a penguin (animal)" in d and "1 of 20" in d and "already counted" in d
    assert g.handle("does it live in water") is None and g.questions == 1  # the same words again: once
    clock.t += 60
    assert "already counted" not in g.describe()
    assert g.handle("is it bigger than a cat") is None and g.questions == 2  # not a guess of "a cat"
    assert g.handle("is it an animal") is None and g.questions == 3
    assert g.handle("this is fun") is None and g.questions == 3  # chat isn't a question


async def test_guesses_are_judged_here(ctx):
    s = store(ctx)
    g = await _game(ctx)
    g.store = s
    r = g.handle("is it a dog?")
    assert r.say == "No, it's not a dog. 19 questions left." and g.questions == 1
    r = g.handle("is it a puppy")
    assert r.say == "No, it's not a puppy. 18 questions left."
    r = g.handle("penguins!")
    assert r.done and r.say.startswith("Yes! It's a penguin! You got it in 3 questions.") and g.won
    assert s.best("twenty_questions")["score"] == 18 and s.best("twenty_questions")["detail"]["questions"] == 3
    assert g.describe().startswith("Voice game: Twenty Questions just ended; the secret was a penguin")


async def test_the_limit_and_the_final_guess(ctx):
    g = await _game(ctx, limit=5)
    for i in range(5):
        g.handle(f"does it fly {i}")
        g.last_counted_at = -1e9
    assert g.questions == 5
    assert "final guess" in g.handle("does it swim").say  # no more questions
    assert "That was the last question" in g.describe()
    r = g.handle("is it a cat")
    assert r.done and r.say.startswith("No, not a cat. I was thinking of a penguin!")


async def test_the_tool_path_counts_and_judges(ctx):
    g = await _game(ctx)
    out = g.answer_tool("does it have feathers")
    assert out.startswith("ok: question 1 of 20 counted. You are thinking of a penguin: answer")
    assert "(already counted)" in g.answer_tool("does it have feathers")
    assert g.answer_tool("is it a duck").startswith("ok: say exactly: No, it's not a duck.")
    assert g.answer_tool("emperor penguin").startswith("ok: say exactly: Yes! It's a penguin!")


async def test_hints_give_up_and_card(ctx):
    g = await _game(ctx)
    assert g.handle("give me a hint").say == "Here's a hint: It's a bird that can't fly, but it's a great swimmer."
    assert g.handle("another hint").say == "Another hint: it starts with P."
    assert g.handle("hint").say == "No more hints! Keep asking."
    spec = g.card_spec()
    json.dumps(spec)
    assert spec["card"] == "game_board" and spec["counter"] == [0, 20] and "animal" in spec["text"]
    r = g.handle("I give up")
    assert r.done and r.say.startswith("I was thinking of a penguin!")
    assert g.card_spec()["text"] == "It was a penguin!"


async def test_stopping_says_the_secret_and_records_nothing(ctx):
    s = store(ctx)
    g = await _game(ctx)
    g.store = s
    g.handle("is it alive")
    r = g.handle("stop the game")
    assert r.done and r.say == "Okay! I was thinking of a penguin."
    assert s.best("twenty_questions") is None
