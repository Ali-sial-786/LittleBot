"""Hangman (littlebot/games/voice/word_game.py): the word list, letters, lives, word guesses, hints, a round."""

from __future__ import annotations

import json

from littlebot.games.voice import data
from littlebot.games.voice.word_game import Hangman, letter_name, pick_category
from voice_game_helpers import rng, store


def _game(ctx, **kw):
    return Hangman(store=store(ctx), rng=rng(kw.pop("seed", 1)), **kw)


def _set_word(g, word, category="animals"):
    g.word, g.category = word, category
    g.guessed, g.wrong_letters, g.lives, g.hints = [], [], g.max_lives, 0


def test_the_word_list():
    cats = data.load("words")["categories"]
    words = [w for ws in cats.values() for w in ws]
    assert len(words) >= 150 and len(set(words)) == len(words) and len(cats) >= 8
    assert all(w.isalpha() and w.isupper() and 4 <= len(w) <= 13 for w in words)
    assert pick_category("Animals", list(cats)) == "animals" and pick_category("animal", list(cats)) == "animals"
    assert pick_category("planets", list(cats)) == "space" and pick_category("zzz", list(cats)) == ""


async def test_letters_and_lives(ctx):
    g = _game(ctx, words=2)
    r = await g.start()
    assert r.say.startswith("Hangman! 2 words, 6 lives each.") and "letters" in r.say
    _set_word(g, "ELEPHANT")
    assert g.handle("E").say == "Yes, two E's!"
    assert g.handle("is there an L").say == "Yes, there's an L."
    assert g.handle("the letter T").say == "Yes, there's a T."
    assert g.handle("E").say == "You already tried E."
    assert g.handle("is there an S").say == "No S. 5 lives left."
    assert g.pattern() == ["E", "L", "E", "", "", "", "", "T"]
    assert g.view["wrong"] == ["S"] and g.view["lives"] == 5 and g.view["last"]["letter"] == "S"
    assert g.handle("this is a hard one") is None  # chat, for the AI
    assert g.handle("hmm") is None


async def test_word_guesses(ctx):
    g = _game(ctx, words=3)
    await g.start()
    _set_word(g, "ELEPHANT")
    assert g.handle("is it a dog") is None  # 3 letters for an 8-letter word: a question for the AI, not a guess
    r = g.handle("is it gorillas")  # about the right length: a guess, and wrong
    assert r.say == "Nope, it's not Gorillas. 5 lives left."
    assert g.handle("is it an animal") is None  # the category, not a guess
    r = g.handle("elephant")
    assert r.say.startswith("Yes! It's Elephant! Word 2:") and g.score == 1 and g.result["won"] is True
    _set_word(g, "PENGUIN")
    r = g.handle("penguins")  # a slip is fine for a long word
    assert r.say.startswith("Yes! It's Penguin!")


async def test_running_out_of_lives_and_a_full_round(ctx):
    s = store(ctx)
    g = Hangman(store=s, rng=rng(), words=2)
    await g.start()
    _set_word(g, "CAT")
    for letter in "BDEFGH":
        r = g.handle(letter)
    assert r.say.startswith("No H. Out of lives! The word was Cat. Word 2:") and g.score == 0
    assert g.result == {"id": 1, "word": "CAT", "won": False}
    _set_word(g, "DOG")
    g.handle("D")
    g.handle("O")
    r = g.handle("G")
    assert r.done and "You got it: Dog!" in r.say and "You solved 1 of 2 words." in r.say
    best = s.best("hangman")
    assert best["score"] == 1 and best["out_of"] == 2


async def test_hints_skip_and_stop(ctx):
    g = _game(ctx, words=5)
    await g.start()
    _set_word(g, "BANANA")
    r = g.handle("give me a hint")
    assert r.say.startswith("Here's a letter: there's") and len(g.guessed) == 1
    g.handle("hint")
    assert "No more hints" in g.handle("hint").say
    r = g.handle("skip")
    assert r.say.startswith("The word was Banana. Word 2:")
    r = g.handle("stop playing")
    assert r.done and r.say.startswith("Okay, we'll stop. The word was")


async def test_repeat_score_and_what_the_ai_sees(ctx):
    g = _game(ctx, words=3)
    await g.start()
    _set_word(g, "MOOSE")
    g.handle("o")
    g.handle("x")
    assert g.handle("repeat").say.endswith("So far: blank O O blank blank. You've tried O, X. 5 lives left.")
    assert g.handle("how many lives do i have").say == "First word! 5 lives left."
    d = g.describe()
    assert "_ O O _ _" in d and "never give the word away" in d and "MOOSE" not in d
    json.dumps(g.card_spec())


def test_letter_names():
    assert letter_name("S") == "an S" and letter_name("T") == "a T" and letter_name("H") == "an H"
