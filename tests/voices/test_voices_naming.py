"""A grown-up saying who they are in a voice chat (littlebot/voices/naming.py): "I'm Ali", "it's her dad"."""

from __future__ import annotations

import pytest

from littlebot.voices.naming import claimed

PEOPLE = [{"id": 1, "name": "Ali", "role": "dad"}, {"id": 2, "name": "Sara", "role": "mum"},
          {"id": 3, "name": "Amara", "role": "the baby"}, {"id": 4, "name": "Nadia", "role": "grandma"}]


@pytest.mark.parametrize("text, who", [
    ("I'm Ali", "Ali"),
    ("Hi LittleBot, it's Ali.", "Ali"),
    ("this is Ali", "Ali"),
    ("I am her dad Ali", "Ali"),
    ("I'm Amara's dad", "Ali"),
    ("It's me, Ali!", "Ali"),
    ("I am her mum", "Sara"),
    ("it's daddy", "Ali"),
    ("I'm Sara, her mum", "Sara"),
    ("I'm her dad, sorry", "Ali"),
    ("Okay. It's Ali here. What's the weather?", "Ali"),
    ("It's granny", "Nadia"),
    ("my name is Sara", "Sara"),
    ("I’m Ali", "Ali"),  # a curly apostrophe, as transcripts often have
])
def test_someone_on_the_list_saying_who_they_are(text, who):
    claim = claimed(text, PEOPLE)
    assert claim is not None and claim.known and claim.person["name"] == who and claim.name == who


@pytest.mark.parametrize("text, name", [
    ("I'm Sam.", "Sam"),
    ("It's Sam speaking", "Sam"),
    ("my name is Sam Jones", "Sam Jones"),
])
def test_a_name_that_isnt_on_the_list(text, name):
    claim = claimed(text, PEOPLE)
    assert claim is not None and not claim.known and claim.name == name


@pytest.mark.parametrize("text", [
    "I'm tired", "It's Monday", "This is Ali's cup", "i'm sam", "Is it Ali?", "Ali is here",
    "It's Sam cooking dinner", "I'm LittleBot", "It's raining", "I'm her uncle", "I am going to the shop",
    "Hello LittleBot", "", "This is fun",
])
def test_sentences_that_dont_say_who_is_talking(text):
    assert claimed(text, PEOPLE) is None


def test_two_people_of_one_name_are_told_apart_by_role_or_not_at_all():
    people = [{"id": 1, "name": "Sam", "role": "dad"}, {"id": 2, "name": "Sam", "role": "uncle"}]
    assert claimed("I'm Sam", people) is None
    assert claimed("I'm Sam, her uncle", people).person["id"] == 2
    two_dads = [{"id": 1, "name": "Ali", "role": "dad"}, {"id": 2, "name": "Omar", "role": "dad"}]
    assert claimed("It's her dad", two_dads) is None  # which one?
