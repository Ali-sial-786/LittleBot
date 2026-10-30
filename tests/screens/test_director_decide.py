"""The screen director with a decision model (littlebot/decide): the card choice (feature cards), "has LittleBot
answered yet?" (answered) and "is this still about the web page?" (browser), on and in shadow."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from test_director import FACT, WEATHER, FakeCard, Setup, make  # noqa: F401 - make is a fixture

from littlebot.decide import Answer, get_decider
from littlebot.decide.base import Choice
from littlebot.screens import director as director_mod
from littlebot.testing import decision_answers, use_decision_model


def picks(value: str, confidence: float = 0.9, *, answered: float = 0.95) -> Any:
    """The card choice answers ``value``; "has it answered?" answers ``answered``."""
    def answer(state: Any, questions: dict[str, Any]) -> dict[str, Answer]:
        return {name: Answer("choice", value, confidence) if isinstance(q, Choice)
                else Answer("yesno", answered, abs(2 * answered - 1)) for name, q in questions.items()}
    return answer


async def asked(s: Setup, question: str, answer: str) -> None:
    s.director.heard(question)
    s.director.said(answer)
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.1)
    await s.settle()


# ---------------------------------------------------------------- cards
async def test_on_no_card_needs_no_text_model_request(make, ctx):
    s = make(FACT, wait_for_answer=True)
    model = use_decision_model(ctx, picks("none"), modes={"cards": "on", "answered": "off"})
    await asked(s, "How do I make pancakes for breakfast?", "Mix flour, eggs and milk, then fry.")
    assert s.provider.calls == [] and s.screens.shown == []
    state, questions = model.asked[0]
    options = questions["screen"].options
    assert list(options)[:1] == ["none"] and {"show_weather", "show_recipe", "show_timer"} <= set(options)
    assert "close" not in options  # no card is up
    assert state["LittleBot_answer_being_said"].startswith("Mix flour")
    assert get_decider(ctx).status()["features"]["cards"]["used"] == 1


async def test_on_an_answer_still_being_said_is_given_as_its_first_words(make, ctx):
    # on the robot the cut-off words read as "hasn't answered yet": none, and no card (2026-10-29)
    s = make(FACT, wait_for_answer=True)
    model = use_decision_model(ctx, picks("show_facts"), modes={"cards": "on", "answered": "off"})
    s.director.heard("Tell me the difference between Genghis Khan and Kublai Khan")
    s.director.saying("Genghis Khan was the founder of the Mongol Empire. He united the Mongol tribes and started "
                      "the big conquests across Asia, and Kublai Khan was his grandson who")
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.1)
    await s.settle()
    state, _questions = model.asked[0]
    assert "LittleBot_answer_being_said" not in state
    assert state["LittleBot_answer_first_words"].endswith("(still being said: LittleBot is answering, the rest follows)")
    assert "a facts card fits" in s.provider.calls[0]["messages"][-1]["content"]


async def test_on_a_card_kind_is_a_hint_for_the_text_model(make, ctx):
    s = make(FACT, wait_for_answer=True)
    use_decision_model(ctx, picks("show_recipe"), modes={"cards": "on", "answered": "off"})
    await asked(s, "How do I make pancakes for breakfast?", "Mix flour, eggs and milk, then fry.")
    content = s.provider.calls[0]["messages"][-1]["content"]
    assert "a recipe card fits" in content and s.screens.shown[0][0]["card"] == "recipe"
    assert get_decider(ctx).status()["features"]["cards"]["agreed"] == 1


async def test_a_new_question_with_a_card_up_gets_a_new_card_not_a_page_turn(make, ctx):
    # on the robot (2026-10-29): "and Alexander the Great?" after a Khans card only turned that card's page
    nxt = {"do": "command", "action": "next", "args": {}}
    new = {"do": "show", "spec": {"card": "recipe", "title": "Crepes", "steps": ["Mix", "Fry thin"]}}
    s = make(nxt, new)
    s.screens.card = FakeCard("recipe", "recipe: Pancakes")
    use_decision_model(ctx, picks("show_recipe"), modes={"cards": "on", "answered": "off"})
    await asked(s, "And how do I make crepes?", "Crepes are thin: mix a runny batter and fry it thin.")
    assert "show a new recipe card for it rather than steering" in s.provider.calls[0]["messages"][-1]["content"]
    assert "Not a command: they asked about something new" in s.provider.calls[1]["messages"][-1]["content"]
    assert s.screens.commands == [] and s.screens.shown[0][0]["title"] == "Crepes"


async def test_on_close_takes_the_card_away(make, ctx):
    s = make(FACT, wait_for_answer=True)
    s.screens.card = FakeCard("weather", "Sunny tomorrow")
    use_decision_model(ctx, picks("close"), modes={"cards": "on", "answered": "off"})
    await asked(s, "How do I make pancakes for breakfast?", "Mix flour, eggs and milk, then fry.")
    assert s.provider.calls == [] and s.screens.closed == ["asked in the voice chat"]


async def test_on_but_unsure_is_the_text_models_decision(make, ctx):
    s = make(FACT, wait_for_answer=True)
    use_decision_model(ctx, picks("none", confidence=0.2), modes={"cards": "on", "answered": "off"})
    await asked(s, "How do I make pancakes for breakfast?", "Mix flour, eggs and milk, then fry.")
    assert len(s.provider.calls) == 1 and s.screens.shown[0][0]["card"] == "recipe"
    assert get_decider(ctx).status()["features"]["cards"]["differed"] == 1


async def test_shadow_changes_nothing_and_logs_both(make, ctx, caplog):
    s = make(WEATHER, wait_for_answer=True)
    use_decision_model(ctx, picks("none"), modes={"cards": "shadow", "answered": "off"})
    with caplog.at_level(logging.INFO, logger="littlebot.decide"):
        await asked(s, "What's the weather like tomorrow?", "Sunny and 20 degrees tomorrow.")
    assert s.screens.shown[0][0]["card"] == "weather"
    assert "cards (shadow): LittleBot's way show_weather, jev none, different" in caplog.text


async def test_off_asks_nothing(make, ctx):
    s = make(FACT, wait_for_answer=True)
    model = use_decision_model(ctx, picks("none"), modes={"cards": "off", "answered": "off"})
    await asked(s, "How do I make pancakes for breakfast?", "Mix flour, eggs and milk, then fry.")
    assert model.asked == [] and len(s.provider.calls) == 1


# ---------------------------------------------------------------- answered
async def test_on_a_stalling_line_the_word_list_misses_isnt_the_answer(make, ctx):
    s = make(FACT, wait_for_answer=True)

    def answer(state: Any, questions: dict[str, Any]) -> dict[str, Answer]:
        if "answered" in questions:
            p = 0.05 if "working it out" in state["LittleBot_said"] else 0.97
            return {"answered": Answer("yesno", p, abs(2 * p - 1))}
        return {}

    use_decision_model(ctx, answer, modes={"answered": "on", "cards": "off"})
    s.director.heard("How do I make pancakes for breakfast?")
    s.director.said("Ooh, I'm working it out for you!")
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.3)
    assert s.provider.calls == []  # it only said it was thinking
    s.director.said("Mix flour, eggs and milk, then fry them.")
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.2)
    await s.settle()
    content = s.provider.calls[0]["messages"][-1]["content"]
    assert "working it out" in content  # (the conversation keeps every line)
    st = get_decider(ctx).status()["features"]["answered"]
    assert st["used"] == 1 and st["agreed"] == 1


async def test_shadow_answered_only_compares(make, ctx):
    s = make(FACT, wait_for_answer=True)
    use_decision_model(ctx, decision_answers("x", 0.05), modes={"answered": "shadow", "cards": "off"})
    await asked(s, "How do I make pancakes for breakfast?", "Ooh, I'm working it out for you!")
    assert len(s.provider.calls) == 1  # the word list's way: it counts as the answer
    assert get_decider(ctx).status()["features"]["answered"]["differed"] == 1


# ---------------------------------------------------------------- browser
class Browser:
    active = True

    def __init__(self) -> None:
        self.moved = 0

    def offered(self) -> bool:
        return True

    def moved_on(self) -> None:
        self.moved += 1

    def status(self) -> dict[str, Any]:
        return {"showing": "Giraffe - Wikipedia"}


@pytest.mark.parametrize(("p", "moved"), [(0.95, 0), (0.1, 1)])
async def test_on_a_turn_about_the_page_doesnt_count_as_moving_on(make, ctx, p, moved):
    s = make({"do": "none"})
    ctx.browser = Browser()
    model = use_decision_model(ctx, decision_answers("x", p), modes={"browser": "on"})
    s.director.heard("how tall do they grow?")
    await s.settle()
    assert ctx.browser.moved == moved
    assert model.asked[0][0]["web_page_on_the_screen"] == "Giraffe - Wikipedia"


async def test_shadow_browser_moves_on_as_before(make, ctx):
    s = make({"do": "none"})
    ctx.browser = Browser()
    use_decision_model(ctx, decision_answers("x", 0.95), modes={"browser": "shadow"})
    s.director.heard("how tall do they grow?")
    await s.settle()
    assert ctx.browser.moved == 1 and get_decider(ctx).status()["features"]["browser"]["differed"] == 1
