"""Whose phone a photo goes to, from the words (littlebot/capture/recipients.py)."""

from __future__ import annotations

import pytest

from littlebot.capture.recipients import target, words_of

ALI = {"id": 1, "name": "Ali", "role": "dad"}
SARA = {"id": 2, "name": "Sara", "role": "mum"}
AMARA = {"id": 3, "name": "Amara", "role": "the baby"}
NANA = {"id": 4, "name": "Ruth", "role": "Amara's grandma"}
FAMILY = [ALI, SARA, AMARA, NANA]


def child(name: str, role: str) -> bool:
    return "baby" in role


def who(said: str, speaker: int | None = None, people: list | None = None) -> tuple[str, int | None]:
    t = target(said, FAMILY if people is None else people, speaker_id=speaker, is_child=child)
    return t.kind, (t.person["id"] if t.person else None)


def test_words() -> None:
    assert words_of("my wife's phone") == ["my", "wife"]
    assert words_of("To Ali’s iPhone, please.") == ["ali"]
    assert words_of("everyone's phones") == ["everyone"]


@pytest.mark.parametrize("said,speaker,expect", [
    ("", 1, ("person", 1)), ("my phone", 2, ("person", 2)), ("mine", None, ("me", None)), ("me", None, ("me", None)),
    ("Ali's", None, ("person", 1)), ("to Sara", 1, ("person", 2)), ("Alis phone", None, ("person", 1)),
    ("my wife's", 1, ("person", 2)), ("my wife", None, ("person", 2)), ("my husband's phone", 2, ("person", 1)),
    ("my hubby", None, ("person", 1)), ("dad's phone", 2, ("person", 1)), ("mom", None, ("person", 2)),
    ("grandma's", None, ("person", 4)), ("everyone", None, ("everyone", None)),
    ("the family", 1, ("everyone", None)), ("all of us", None, ("everyone", None)),
    ("our phones", 1, ("everyone", None)), ("both", None, ("everyone", None)),
    ("Amara's", 1, ("unknown", None)),  # the little one has no phone
    ("Bob's", 1, ("unknown", None)), ("the weather", None, ("unknown", None)),
])
def test_targets(said: str, speaker: int | None, expect: tuple[str, int | None]) -> None:
    assert who(said, speaker) == expect


def test_spouse_needs_to_know_who_is_talking_when_roles_dont_say() -> None:
    people = [{"id": 1, "name": "Ali", "role": "parent"}, {"id": 2, "name": "Sara", "role": "parent"}]
    assert who("my spouse's phone", None, people) == ("needs_speaker", None)
    assert who("my spouse's phone", 1, people) == ("person", 2)
    assert who("my wife", 1, people) == ("person", 2)  # the roles don't say who's who: the other parent
    assert who("my partner", 2, people) == ("person", 1)
    assert who("my spouse", 1, FAMILY) == ("person", 2)  # not grandma: she isn't a parent


def test_two_people_fit() -> None:
    people = [ALI, {"id": 5, "name": "Ali", "role": "uncle"}, SARA]
    assert who("Ali's", None, people) == ("unclear", None)
    assert who("dad", 2, [ALI, SARA, {"id": 6, "name": "Joe", "role": "Sara's dad"}]) == ("unclear", None)
    # the one talking is left out when a role fits them and someone else
    assert who("dad", 1, [ALI, SARA, {"id": 6, "name": "Joe", "role": "Ali's dad"}]) == ("person", 6)
