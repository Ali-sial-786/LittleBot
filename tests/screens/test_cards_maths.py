"""The maths and counting cards (littlebot/screens/cards/maths.py): checked results, no eval, counting."""

from __future__ import annotations

import pytest

from littlebot.screens import base, preview
from littlebot.screens.cards import maths
from littlebot.screens.cards.maths import (
    CountCard,
    MathsCard,
    check,
    convert,
    evaluate,
    format_number,
    pretty,
)


def test_registered_with_fields_actions_and_samples():
    ct = base.card_types()["maths"]
    assert set(ct.fields) == {"expression", "result", "steps"} and ct.required == ("expression", "result")
    assert set(ct.actions) == {"show"}
    count = base.card_types()["count"]
    assert count.required == ("to",) and set(count.actions) == {"again", "count"}
    assert {s["card"] for s in maths.SAMPLES} == {"maths", "count"}


async def test_every_sample_builds_and_draws():
    for spec in maths.SAMPLES:
        c = await base.build(spec, None, base.RecordingHost())
        assert isinstance(c, (MathsCard, CountCard))
        assert preview.surface(c, seconds=2.0).get_size() == (base.W, base.H)


@pytest.mark.parametrize("text, value", [
    ("12 x 7", 84), ("12 × 7", 84), ("12*7", 84), ("(3 + 4) * 2", 14), ("100 / 8", 12.5), ("2^10", 1024),
    ("15% of 80", 12), ("1,000 - 1", 999), ("-3 + 5", 2), ("7 times 6", 42), ("9 divided by 3", 3),
    ("5 squared", 25), ("2 + 3 * 4", 14), ("2 ^ 3 ^ 2", 512), ("12 x 7 =", 84), (".5 + .25", 0.75),
])
def test_evaluate(text, value):
    assert evaluate(text) == pytest.approx(value)


@pytest.mark.parametrize("text", [
    "__import__('os').system('ls')", "1 / 0", "2 ^ 1000", "3 cups", "", "1 +", "(1 + 2", "abc", "1e400",
    "9" * 100, "2 ** 2 ** 2 ** 2 ** 2",
])
def test_evaluate_refuses_anything_else(text):
    assert evaluate(text) is None


def test_format_number():
    assert format_number(84.0) == "84" and format_number(1234567) == "1,234,567"
    assert format_number(3.14159) == "3.142" and format_number(0.125) == "0.125" and format_number(2.5, 2) == "2.5"


def test_check_corrects_wrong_sums_and_trusts_the_rest():
    assert check("12 x 7", "84") == "84"
    assert check("12 x 7", "86") == "84"
    assert check("22 / 7", "3.14") == "3.14"  # rounded right
    assert check("22 / 7", "3.5") == "3.1"  # wrong: the right one, to the places it gave
    assert check("10 / 4", "2.5") == "2.5" and check("12 x 7", "= 84") == "= 84"
    assert check("12 x 7", "about eighty") == "about eighty"  # words: trusted
    assert check("1000 x 1000", "1,000,000") == "1,000,000"


def test_check_conversions():
    assert check("3 cups to ml", "710 ml") == "710 ml"
    assert check("3 cups", "900 ml") == "710 ml"
    assert check("350 °F to °C", "177 °C") == "177 °C"
    assert check("350 F", "200 C") == "177 C"
    assert check("5 km in miles", "3.1 miles") == "3.1 miles"
    assert check("1 lb", "454 g") == "454 g" and check("1 lb", "500 g") == "454 g"
    assert check("2 cups of flour", "250 g") == "250 g"  # cups to grams: can't be checked here
    assert check("1 smoot", "1.7 m") == "1.7 m"
    assert convert(100, "°C", "°F") == pytest.approx(212) and convert(1, "kg", "lbs") == pytest.approx(2.2046, 1e-3)
    assert convert(60, "mph", "km/h") == pytest.approx(96.56, 1e-3) and convert(1, "kg", "ml") is None


def test_pretty():
    assert pretty("12*7") == "12 × 7" and pretty("12 x 7") == "12 × 7" and pretty("100/8") == "100 ÷ 8"
    assert pretty("3+4") == "3 + 4" and pretty("350 °F to °C") == "350°F to °C" and pretty("60 km/h") == "60 km/h"


def test_maths_card():
    host = base.RecordingHost()
    c = MathsCard({"expression": "12 x 7", "result": "86"}, host)
    assert c.describe() == "12 × 7 = 84" and c.title == "Let's work it out"
    c = MathsCard({"expression": "23 × 14", "result": "322", "steps": ["a", "b", "c", "d"]}, host)
    assert c.steps == ["a", "b", "c"]
    assert c.command("show", {"expression": "12 x 8"}) and c.describe() == "12 × 8 = 96" and c.steps == []
    assert not c.command("show", {}) and not c.command("next", {})
    conv = MathsCard({"expression": "3 cups to ml", "result": "710 ml"}, host)
    assert conv.title == "Converting" and conv.describe() == "3 cups to ml = 710 ml"


async def test_maths_needs_an_expression_and_a_result():
    host = base.RecordingHost()
    assert await base.build({"card": "maths", "expression": "1 + 1"}, None, host) is None
    assert await base.build({"card": "maths", "result": "2"}, None, host) is None


def test_count_card_pops_things_one_by_one():
    host = base.RecordingHost()
    c = CountCard({"to": 3, "thing": "apples"}, host)
    assert c.title == "Let's count to 3!" and c.thing == "apple" and c.describe() == "Counting to 3: 0 apples so far"
    for _ in range(int(1.0 * 30)):
        c.update(1 / 30)
    assert c.shown == 1 and host.sounds == ["pop"]
    for _ in range(int(3.0 * 30)):
        c.update(1 / 30)
    assert c.shown == 3 and host.sounds == ["pop", "pop", "pop", "sparkle"] and c.describe() == "3 apples, all counted"
    preview.surface(c, seconds=0.1)
    assert c.command("again", {}) and c.shown == 0
    assert c.command("count", {"to": 12}) and c.n == 10 and c.title == "Let's count to 10!"
    preview.surface(c, seconds=12)
    assert c.shown == 10
    assert CountCard({"to": "lots", "thing": "unicorn"}, host).n == 5
    assert CountCard({"to": 1}, host).describe() == "Counting to 1: 0 stars so far"
