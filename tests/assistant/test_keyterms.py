"""The words speech-to-text listens harder for (littlebot/assistant/keyterms.py)."""

from __future__ import annotations

from typing import Any

from littlebot.assistant.keyterms import MAX_CHARS, MAX_TERMS, fit, hearing_terms, recent_names
from littlebot.testing import make_test_context
from littlebot.vision.faces import PeopleStore


class Hub:
    def __init__(self, words: list[str]) -> None:
        self.words = words

    def hearing_words(self) -> list[str]:
        return list(self.words)


def _ctx(**cfg: Any) -> Any:
    ctx = make_test_context(**cfg)
    ctx.settings.set("profile", {"nickname": "Amara"})
    PeopleStore(ctx.db).add_person("Sana", "mom")
    ctx.skills = Hub(["Bedtime Bops", "Taylor Swift", "Kitchen Lamp"])
    return ctx


def test_the_familys_world_in_order_after_the_fixed_words():
    ctx = _ctx(ai={"creator": "Ali", "creator_relation": "Amara's dad"})
    history = [{"role": "user", "content": "how long is the great wall"},
               {"role": "assistant", "content": "The Great Wall of China is about 21,000 kilometres long."}]
    try:
        terms = hearing_terms(ctx, history)
        assert terms[:4] == [ctx.config.robot_name, "louder", "quieter", "volume"]
        assert terms[4:] == ["Ali", "Amara", "Sana", "Bedtime Bops", "Taylor Swift", "Kitchen Lamp",
                             "Great Wall of China"]
    finally:
        ctx.db.close()


def test_nothing_personal_when_switched_off_and_no_child_name_unless_shared():
    ctx = _ctx(assistant={"personal_keyterms": False})
    try:
        assert hearing_terms(ctx) == [ctx.config.robot_name, "louder", "quieter", "volume"]
    finally:
        ctx.db.close()
    ctx = _ctx(ai={"share_child_name": False})
    try:
        terms = hearing_terms(ctx)
        assert "Amara" not in terms and "Sana" in terms
    finally:
        ctx.db.close()


def test_names_from_the_last_turns_not_ordinary_capitals():
    history = [{"role": "user", "content": "Sure. Play some Taylor Swift for Amara please"},
               {"role": "assistant", "content": "Okay! Playing Taylor Swift. Rain is coming on Monday, so "
                                                "stay in. I put on Shake It Off."}]
    names = recent_names(history)
    assert names == ["Taylor Swift", "Shake It Off", "Amara"]  # newest first; "Okay", "Rain", "Monday", "I" left out


def test_the_limits_hold():
    many = [f"Name{i}" for i in range(80)]
    assert len(fit(many)) == MAX_TERMS
    assert fit(["Amara", "amara", " Amara. ", "x", "a" * 60, "Bedtime  Bops"]) == ["Amara", "Bedtime Bops"]
    long = [f"{'Word' * 9}{i:03d}" for i in range(40)]  # 39 characters each
    assert sum(len(t) for t in fit(long)) <= MAX_CHARS
