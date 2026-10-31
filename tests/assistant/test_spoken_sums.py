"""A sum asked by voice, worked out before the brain answers (littlebot/assistant/spoken_sums.py)."""

from __future__ import annotations

import pytest

from littlebot.assistant.spoken_sums import digits, worked_out


@pytest.mark.parametrize(("said", "written"), [
    ("seventeen sixty four", "1764"), ("twenty three times fourteen", "23 times 14"),
    ("three hundred and forty two divided by six", "342 divided by 6"), ("two thousand and twenty six", "2026"),
    ("twenty twenty six", "2026"), ("one hundred and five", "105"), ("a hundred", "a 100"),
    ("three point five plus point two", "3.5 plus point 2"), ("twenty-one", "21"), ("Fifteen percent of eighty?", "15 percent of 80"),
    ("three four", "three four"), ("two and two", "two and two"), ("one million two hundred thousand", "1200000"),
    ("What is 12 times seven?", "what is 12 times 7"),
])
def test_spoken_numbers_become_digits(said: str, written: str) -> None:
    assert digits(said) == written


@pytest.mark.parametrize(("question", "result"), [
    # "about forty-one point nine" (the robot, 2026-10-31)
    ("What is the square root of seventeen sixty four?", "square root of 1764 = 42"),
    ("What's twenty three times fourteen?", "23 times 14 = 322"),
    ("What is fifteen percent of eighty?", "15 percent of 80 = 12"),
    ("What's three hundred and forty two divided by six?", "342 divided by 6 = 57"),
    ("Hey what is 2 plus 2", "2 plus 2 = 4"),
    ("What's five squared?", "5 squared = 25"),
    ("What is 22 divided by 7?", "22 divided by 7 = 3.143"),
])
def test_a_sum_is_worked_out(question: str, result: str) -> None:
    assert worked_out(question) == result


@pytest.mark.parametrize("question", [
    "What is the capital of France?", "How many cups are in a liter?", "What's seventeen?", "Who was Albert Einstein?",
    "What time is it in Tokyo?", "What's two and two?", "Tell me about the year nineteen eighty four.", "",
    "What is one divided by zero?",
])
def test_anything_else_is_not(question: str) -> None:
    assert worked_out(question) is None
