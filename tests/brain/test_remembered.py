"""The notes a grown-up asks the voice chat to remember (littlebot/brain/remembered.py)."""

from __future__ import annotations

from littlebot.brain import remembered


class FakeSettings:
    def __init__(self, profile=None):
        self.values = {"profile": profile or {}}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, *, source=""):
        self.values[key] = value


def test_notes_are_tidied_kept_once_and_the_oldest_goes_first():
    s = FakeSettings({"nickname": "Amara"})
    assert remembered.remember(s, '  "ali is   Amara\'s dad."  ') == "Ali is Amara's dad"
    assert remembered.remember(s, "Ali is amara's dad") == "Ali is amara's dad"
    assert remembered.notes(s) == ["Ali is amara's dad"]
    assert s.values["profile"]["nickname"] == "Amara"  # the rest of the profile is untouched
    for i in range(remembered.MAX_NOTES + 3):
        remembered.remember(s, f"note {i}")
    kept = remembered.notes(s)
    assert len(kept) == remembered.MAX_NOTES and kept[-1] == f"Note {remembered.MAX_NOTES + 2}"


def test_empty_or_overlong_notes_are_refused():
    s = FakeSettings()
    assert remembered.remember(s, "  . ") is None
    assert remembered.remember(s, "x" * (remembered.MAX_LEN + 1)) is None
    assert remembered.notes(s) == []


def test_forget_drops_the_notes_it_is_about():
    s = FakeSettings({"remembered": ["Ali is Amara's dad", "Amara loves ducks"]})
    assert remembered.forget(s, "Ali is the dad") == ["Ali is Amara's dad"]
    assert remembered.forget(s, "the weather") == []
    assert remembered.notes(s) == ["Amara loves ducks"]
