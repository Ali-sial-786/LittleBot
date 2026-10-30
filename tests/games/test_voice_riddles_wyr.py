"""Riddles and Would You Rather (littlebot/games/voice/riddles.py, would_you_rather.py)."""

from __future__ import annotations

import json

from littlebot.games.voice import answers, data
from littlebot.games.voice.riddles import Riddles
from littlebot.games.voice.would_you_rather import WouldYouRather
from voice_game_helpers import rng, store


def _riddle(g, q_start):
    r = next(x for x in g._load() if x["q"].startswith(q_start))
    g.current = r
    g.tries, g.hinted = 0, False
    return r


# ---------------------------------------------------------------- riddles
def test_the_riddles():
    rs = data.items("riddles", "riddles")
    assert len(rs) >= 40 and len({r["q"] for r in rs}) == len(rs)
    for r in rs:
        assert r["hint"] and r["accept"] and r["q"].endswith("?")
        assert answers.matches(r["say"], r["accept"] + [r["say"]]), r["q"]
        assert answers.matches(f"is it {r['accept'][0]}?", r["accept"]), r["q"]
        assert not answers.matches("a banana sandwich", r["accept"]), r["q"]


async def test_a_riddle_round(ctx):
    s = store(ctx)
    g = Riddles(store=s, rng=rng(), riddles=2)
    r = await g.start()
    assert r.say.startswith("Riddle time! 2 riddles. Here's the first: ")
    _riddle(g, "What gets wetter")
    r = g.handle("I think it's a towel")
    assert "! A towel. Last one: " in r.say and r.sound == "success" and g.score == 1
    _riddle(g, "I have keys")
    assert g.handle("a door").say == "Nope! Try again, or say hint."
    assert g.handle("a car").say == "Not quite. Here's a hint: You type on me."
    r = g.handle("a computer")
    assert r.done and r.say.startswith("Good tries! The answer was: A keyboard.") and "You solved 1 of 2" in r.say
    assert s.best("riddles")["score"] == 1


async def test_chat_is_not_a_guess_and_hints_and_give_up(ctx):
    g = Riddles(store=store(ctx), rng=rng(), riddles=3)
    await g.start()
    _riddle(g, "What has to be broken")
    for chat in ("what do you mean", "hmm", "that's a tough one", "can you say it slower",
                 "I really have absolutely no idea what that could possibly be"):
        assert g.handle(chat) is None, chat
    assert g.tries == 0
    assert g.handle("hint").say == "Hint: You might find a dozen of them in the fridge."
    r = g.handle("I give up")
    assert r.say.startswith("The answer was: An egg. Next one:")
    assert g.result == {"id": 1, "answer": "An egg", "right": False}
    assert '"' in g.describe() and "never give the answer away" in g.describe()
    json.dumps(g.card_spec())
    assert g.card_spec()["title"] == "Riddle 2 of 3"


# ---------------------------------------------------------------- would you rather
def test_the_pairs():
    ps = data.items("would_you_rather", "pairs")
    assert len(ps) >= 40 and len({(p["a"], p["b"]) for p in ps}) == len(ps)
    for p in ps:
        assert p["pick"] in (0, 1) and p["why"] and p["a_short"] and p["b_short"]


async def _wyr(ctx, s=None, a="be able to fly"):
    g = WouldYouRather(store=s or store(ctx), rng=rng(), questions=3)
    await g.start()
    p = next(x for x in g._load() if x["a"] == a)
    g.current = p
    g.tally = g._tally(p)
    return g


async def test_picks_reactions_and_the_tally(ctx):
    s = store(ctx)
    for said in ("fly", "the first one", "flying", "option a"):
        g = await _wyr(ctx, s)
        r = g.handle(said)
        assert r is not None and r.say.startswith("Flying! "), said
        assert "Next:" in r.say
    g = await _wyr(ctx, s)
    r = g.handle("I'd be invisible")
    assert r.say.startswith("Being invisible! ") and "I'd pick flying." in r.say
    assert "Most people here pick flying, 4 to 1." in r.say
    assert s.tally(g.result and data.key_of("be able to fly", "be invisible")) == [4, 1]
    assert g.result["picked"] == 1 and g.result["tally"] == [4, 1] and g.result["mine"] == 0


async def test_both_skip_tap_and_end(ctx):
    g = await _wyr(ctx)
    assert g.handle("both!").say.startswith("Ha, you have to pick one! Flying, or being invisible?")
    assert g.handle("tell me a joke") is None
    r = g.tap(1)
    assert r is not None and g.number == 2
    assert g.tap(5) is None
    r = g.handle("skip")
    assert r.say.startswith("Okay, a different one. Would you rather")
    r = g.tap(0)
    assert r.done and "That was fun!" in r.say and "We agreed on" in r.say
    assert g.over and g.store.best("would_you_rather") is None  # no score kept
    spec = g.card_spec()
    json.dumps(spec)
    assert spec["over"]
