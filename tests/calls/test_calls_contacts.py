"""Who can be called (littlebot/calls/contacts.py): the dashboard's list tidied, and a said name found on it."""

from __future__ import annotations

import pytest

from littlebot.calls import contacts as C

PEOPLE = {"Dad": "Ali Khan", "Mum": "Sara", "Grandma Rose": "Rose Miller", "Sam": "Sam"}


@pytest.mark.parametrize("said,found", [
    ("Dad", ("Dad", "Ali Khan")), ("dad", ("Dad", "Ali Khan")), ("my dad", ("Dad", "Ali Khan")),
    ("Dad's", ("Dad", "Ali Khan")), ("daddy", ("Dad", "Ali Khan")), ("mom", ("Mum", "Sara")),
    ("my mum please", ("Mum", "Sara")), ("Grandma Rose", ("Grandma Rose", "Rose Miller")),
    ("grandma rose on whatsapp", ("Grandma Rose", "Rose Miller")), ("Sam.", ("Sam", "Sam")),
])
def test_find(said: str, found: tuple[str, str]) -> None:
    assert C.find(PEOPLE, said) == found


@pytest.mark.parametrize("said", ["", "Ali Khan", "Rose", "the doctor", "everyone", "grandma", None])
def test_nobody_else_can_be_called(said: object) -> None:
    """Only the said names on the list: not the WhatsApp names, not a part of one."""
    assert C.find(PEOPLE, said) is None


def test_clean_tidies_the_dashboards_list() -> None:
    kept = C.clean([{"name": "  Dad ", "whatsapp": "Ali​   Khan"}, {"name": "Mum", "whatsapp": ""},
                    {"name": "", "whatsapp": ""}])
    assert kept == {"Dad": "Ali Khan", "Mum": "Mum"}
    assert C.clean({"Dad": "Ali"}) == {"Dad": "Ali"}
    assert C.clean([]) == {}


@pytest.mark.parametrize("raw,words", [
    ([{"name": "", "whatsapp": "Ali"}], "needs the name you say"),
    ([{"name": "Dad", "whatsapp": "a"}, {"name": "my dad", "whatsapp": "b"}], "twice"),
    ([{"name": "x" * 41, "whatsapp": ""}], "40 characters"),
    ([{"name": "!!!", "whatsapp": ""}], "letters"),
    ([{"name": f"p{i}", "whatsapp": ""} for i in range(31)], "up to 30"),
    ("Dad", "weren't understood"), (["Dad"], "needs a name"),
])
def test_clean_says_what_is_wrong(raw: object, words: str) -> None:
    with pytest.raises(ValueError, match=words):
        C.clean(raw)


def test_who_can_be_called_in_words() -> None:
    assert C.say_who(PEOPLE) == "Dad, Mum, Grandma Rose or Sam"
    assert C.say_who({"Dad": "x"}) == "Dad" and C.say_who({}) == ""
    assert C.spoken(PEOPLE) == ["Dad", "Mum", "Grandma Rose", "Sam"]
