"""The voice games' local answer matcher (littlebot/games/voice/answers.py): letters, ordinals, numbers, fuzzy
words, negation, spoken letters for hangman, and robustness on odd input."""

from __future__ import annotations

import pytest

from littlebot.games.voice import answers as A

PLANETS = ["Mars", "Jupiter", "Leonardo da Vinci", "The Pacific Ocean"]


@pytest.mark.parametrize("said, want", [
    ("B", 1), ("b", 1), ("bee", 1), ("option b", 1), ("answer B", 1), ("letter b", 1), ("um I think it's B", 1),
    ("is it b?", 1), ("B please", 1), ("b, final answer", 1), ("I'll go with C", 2), ("I'll go for c", 2),
    ("see", 2), ("sea", 2), ("dee", 3), ("D!", 3), ("a", 0), ("A.", 0), ("b as in boy", 1), ("be as in boy", 1),
    ("the second one", 1), ("second", 1), ("the first option", 0), ("the last one", 3), ("number two", 1),
    ("two", 1), ("2", 1), ("number 4", 3), ("four", 3),
])
def test_letters_ordinals_and_positions(said, want):
    assert A.match_choice(said, PLANETS) == want


@pytest.mark.parametrize("said, want", [
    ("jupiter", 1), ("Jupiter!", 1), ("it's the planet jupiter I guess", 1), ("jupitor", 1), ("da vinci", 2),
    ("leonardo", 2), ("Leonardo da Vinci", 2), ("pacific", 3), ("the pacific", 3), ("b jupiter", 1),
    ("d, jupiter", 1),  # the words win over a misheard letter
])
def test_option_words_forgivingly(said, want):
    assert A.match_choice(said, PLANETS) == want


@pytest.mark.parametrize("said", ["a or b", "not b", "it's not jupiter", "paris", "a cat", "this is hard",
                                  "mars or jupiter", "", "   ", "hmm let me think about it for a while"])
def test_not_an_answer(said):
    assert A.match_choice(said, PLANETS) is None


def test_numbers_as_values_beat_positions():
    opts = ["6", "8", "10", "12"]
    assert A.match_choice("eight", opts) == 1
    assert A.match_choice("8", opts) == 1
    assert A.match_choice("twelve", opts) == 3
    assert A.match_choice("two", opts) == 1  # no option is 2: the second one
    assert A.match_choice("two", ["1", "2", "3", "4"]) == 1  # the option "2"
    assert A.match_choice("four", ["2", "4", "6", "8"]) == 1  # the value, not the fourth
    years = ["1912", "1905", "1921", "1898"]
    assert A.match_choice("nineteen twelve", years) == 0
    assert A.match_choice("1921", years) == 2  # never "close" to 1912
    assert A.match_choice("three sixty six", ["366", "365", "364", "360"]) == 0
    assert A.match_choice("fourteen forty", ["1,440", "1,240", "1,400", "2,400"]) == 0
    assert A.match_choice("one thousand four hundred and forty", ["1,440", "1,240", "1,400", "2,400"]) == 0
    assert A.match_choice("eight minutes", ["8 minutes", "8 seconds", "8 hours", "1 minute"]) == 0
    assert A.match_choice("eight", ["8 minutes", "8 seconds", "8 hours", "1 minute"]) is None  # which one?
    assert A.match_choice("42", ["42 kilometres", "21 kilometres", "50 kilometres", "30 kilometres"]) == 0


def test_two_options_and_odd_inputs():
    assert A.match_choice("fly", ["be able to fly", "be invisible"]) == 0
    assert A.match_choice("invisible", ["be able to fly", "be invisible"]) == 1
    assert A.match_choice("c", ["x", "y"]) is None  # only A and B exist
    for bad in (None, 3, {"a": 1}, ["b"], "x" * 5000):
        assert A.match_choice(bad, PLANETS) is None or isinstance(A.match_choice(bad, PLANETS), int)
    assert A.match_choice("b", []) is None


@pytest.mark.parametrize("said, want", [
    ("twenty one pilots", "21 pilots"), ("a hundred and five", "105"), ("nineteen oh five", "1905"),
    ("two thousand and one", "2001"), ("the answer is the nile please", "nile"), ("um, ok, I think it's B", "b"),
    ("It’s Café Olé!", "cafe ole"), ("rock & roll", "rock and roll"), ("one", "1"),
])
def test_core(said, want):
    assert A.core(said) == want


def test_close_and_edit_distance():
    assert A.edit_distance("kitten", "sitting") == 3
    assert A.edit_distance("abc", "abc") == 0
    assert A.edit_distance("a" * 10, "b" * 10, limit=2) == 3  # stopped early
    assert A.close("jupitor", "jupiter") and A.close("ice cream", "icecream")
    assert not A.close("cat", "car")  # three letters: exact only
    assert not A.close("1912", "1921")
    assert not A.close("", "x")


@pytest.mark.parametrize("said, accepted, want", [
    ("I think it's a towel", ["towel"], True), ("a towel?", ["towel"], True), ("towels", ["towel"], True),
    ("not a towel", ["towel"], False), ("a sponge", ["towel"], False), ("tea pot", ["teapot"], True),
    ("the letter m", ["m", "the letter m"], True), ("all of them", ["all of them", "all"], True),
    ("there are no stairs", ["no stairs"], True), ("are you asleep", ["are you asleep"], True), ("", ["x"], False),
])
def test_matches(said, accepted, want):
    assert A.matches(said, accepted) is want


@pytest.mark.parametrize("said, want", [
    ("E", "e"), ("e", "e"), ("is there an S", "s"), ("is there an ess", "s"), ("the letter T", "t"),
    ("letter tee", "t"), ("T as in Tom", "t"), ("are there any e's", "e"), ("how about R", "r"), ("you", "u"),
    ("double you", "w"), ("why", "y"), ("oh", "o"), ("letter bee", "b"), ("I think there's an E", "e"),
    ("is it e", "e"), ("maybe a", "a"), ("zed", "z"), ("try p please", "p"), ("do you have a Q", "q"),
])
def test_spoken_letters(said, want):
    assert A.spoken_letter(said) == want


@pytest.mark.parametrize("said", ["elephant", "hmm", "is it elephant", "", None, 42, ["e"], "letters"])
def test_not_a_letter(said):
    assert A.spoken_letter(said) is None


def test_a_question_about_an_option_is_not_a_pick():
    symbols = ["Au", "Ag", "Gd", "Go"]
    assert A.match_choice("what does Au mean?", symbols) is None
    assert A.match_choice("why would it be Ag", symbols) is None
    assert A.match_choice("Au", symbols) == 0 and A.match_choice("what about B", symbols) == 1
    assert A.match_choice("how about jupiter", PLANETS) == 1 and A.match_choice("is it jupiter?", PLANETS) == 1
    assert A.match_choice("which one is jupiter", PLANETS) is None
    assert not A.matches("what is a towel made of", ["towel"])
    assert A.matches("is it a towel", ["towel"])
