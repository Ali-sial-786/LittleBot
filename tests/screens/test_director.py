"""The screen director (littlebot/screens/director.py): what the screen shows during a voice chat, decided from
what was just asked, against a fake text model and a fake screen service (no network, no display)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from littlebot.brain.providers.base import ProviderError, ProviderReply
from littlebot.screens import director as director_mod
from littlebot.screens.base import CardType
from littlebot.screens.director import (
    CLOSED_NOTE,
    GAMES_NOTE,
    ScreenDirector,
    clean_args,
    clean_spec,
    local_rule,
    parse_decision,
    prefilter,
    real_words,
)


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> None:
    return None


TYPES = {
    "weather": CardType("weather", "The weather now and the next days, at home or somewhere else.",
                        {"place": {"type": "string"}, "day": {"type": "integer", "minimum": 0, "maximum": 6}},
                        _build, actions={"day": "offset (int, 0 today)"}),
    "recipe": CardType("recipe", "A recipe, one step at a time.",
                       {"title": {"type": "string", "maxLength": 60},
                        "steps": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
                        "serves": {"type": "integer", "minimum": 1, "maximum": 20}},
                       _build, required=("title", "steps"), actions={"next": "the next step", "back": "the step before"}),
    "timer": CardType("timer", "A countdown.", {"minutes": {"type": "number", "minimum": 0, "maximum": 180},
                                                "label": {"type": "string"}},
                      _build, required=("minutes",), actions={"stop": "stop it", "pause": "pause it"}),
    "games": CardType("games", "The menu of LittleBot's on-screen games.", {}, _build),
}


class FakeCard:
    def __init__(self, kind: str, desc: str) -> None:
        self.kind, self.desc = kind, desc


class FakeScreens:
    """Stands in for ScreenService (ctx.screens)."""

    def __init__(self) -> None:
        self.card: FakeCard | None = None
        self.shown: list[tuple[dict[str, Any], str]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.closed: list[str] = []
        self.busy = False
        self.unchanged = False  # command() leaves describe() as it was

    @property
    def active(self) -> bool:
        return self.card is not None

    @property
    def current(self) -> FakeCard | None:
        return self.card

    def describe(self) -> str | None:
        return self.card.desc if self.card else None

    async def show(self, spec: dict[str, Any], *, source: str = "voice") -> dict[str, Any]:
        self.shown.append((spec, source))
        if self.busy:
            return {"ok": False, "showing": None, "error": "busy"}
        names = {"games": "Rock Paper Scissors, Bubble Pop, Memory Lights"}
        self.card = FakeCard(spec["card"], names.get(spec["card"]) or
                             f"{spec['card']}: {spec.get('title') or spec.get('place') or 'home'}")
        return {"ok": True, "showing": self.card.desc}

    def command(self, action: str, args: dict[str, Any]) -> bool:
        self.commands.append((action, args))
        if self.card is None:
            return False
        if not self.unchanged:
            self.card.desc = f"{self.card.kind} after {action} {json.dumps(args)}"
        return True

    def close(self, reason: str) -> bool:
        self.closed.append(reason)
        had, self.card = self.card is not None, None
        return had


class JsonProvider:
    """The text model: answers each decision with the next of ``answers`` (the last one repeats)."""

    name = "chatgpt"
    model = "fake"

    def __init__(self, *answers: Any, delay: float = 0.0, error: Exception | None = None) -> None:
        self.answers = list(answers) or [{"do": "none"}]
        self.delay, self.error = delay, error
        self.calls: list[dict[str, Any]] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    effort: str | None = None) -> ProviderReply:
        self.calls.append({"messages": messages, "system": system, "effort": effort})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return ProviderReply(text=answer if isinstance(answer, str) else json.dumps(answer), model="fake")


class Setup:
    def __init__(self, ctx: Any, provider: JsonProvider, **kw: Any) -> None:
        self.ctx, self.provider = ctx, provider
        self.screens = ctx.screens = FakeScreens()
        self.notes: list[str] = []
        self.director = ScreenDirector(ctx, lambda: self.provider, note=self.notes.append, types=lambda: TYPES, **kw)

    async def heard(self, text: str) -> None:
        self.director.heard(text)
        await self.settle()

    async def settle(self) -> None:
        while self.director._tasks:
            await asyncio.gather(*list(self.director._tasks), return_exceptions=True)


@pytest.fixture
def make(ctx):
    ctx.privacy.set(cloud_enabled=True)

    def _make(*answers: Any, **kw: Any) -> Setup:
        provider = JsonProvider(*answers, **{k: kw.pop(k) for k in ("delay", "error") if k in kw})
        return Setup(ctx, provider, **kw)

    return _make


def set_cards_enabled(ctx: Any, on: bool) -> None:
    try:
        ctx.settings.set("screens.enabled", on)
    except KeyError:  # not registered in this checkout (the screen service registers it)
        ctx.settings._values["screens.enabled"] = on


WEATHER = {"do": "show", "spec": {"card": "weather", "day": 1}}


# ---------------------------------------------------------------- the model's way
async def test_a_question_worth_showing_puts_a_card_up_and_tells_the_voice_once(make):
    s = make(WEATHER, {"do": "show", "spec": {"card": "recipe", "title": "Pancakes", "steps": ["Mix", "Fry"]}})
    s.director.said("Hello! What shall we do today?")
    await s.heard("What's the weather like tomorrow?")
    assert s.screens.shown == [({"card": "weather", "day": 1}, "voice")]
    assert len(s.notes) == 1
    assert s.notes[0].startswith("On your screen now: weather: home.") and "Don't read the screen out" in s.notes[0]
    assert "\"close it\"" in s.notes[0] and "\"next\"" not in s.notes[0]  # the weather card has no "next"
    call = s.provider.calls[0]
    assert call["effort"] == "none"  # the camera looks' fast effort
    assert "- weather: The weather now" in call["system"] and "- recipe:" in call["system"]
    assert '"required"' not in call["system"] and "Required: title, steps." in call["system"]
    content = call["messages"][-1]["content"]
    assert content.startswith("Card on screen: none.")
    assert "LittleBot: Hello! What shall we do today?" in content
    assert content.endswith("They just said: What's the weather like tomorrow?\n\n" + director_mod.ANSWER_CUE)
    await s.heard("How do I make pancakes for breakfast?")
    assert s.notes[1] == "On your screen now: recipe: Pancakes."  # brief from now on
    assert [h["what"] for h in s.director.history] == ["show weather", "show recipe"]
    assert all(h["path"] == "model" for h in s.director.history)


async def test_a_follow_up_steers_the_card_instead_of_rebuilding_it(make):
    s = make(WEATHER, {"do": "command", "action": "day", "args": {"offset": 2}})
    await s.heard("What's the weather like tomorrow?")
    await s.heard("and what about the day after?")
    assert len(s.screens.shown) == 1 and s.screens.commands == [("day", {"offset": 2})]
    content = s.provider.calls[1]["messages"][-1]["content"]
    assert content.startswith('Card on screen: weather: "weather: home" (actions: day).')
    assert s.notes[-1] == 'The screen now shows: weather after day {"offset": 2}.'


async def test_most_turns_cost_nothing(make):
    s = make(WEATHER)
    for text in ("hi", "ba ba ba", "mama", "yes please", "How are you?", "how many fingers am I holding up?",
                 "stop", "play the baby shark video", "make a happy face", "sing twinkle twinkle little star",
                 "she is so cute", "say hello to the baby", "I love you", "what is your favourite colour",
                 "who am I", ""):
        await s.heard(text)
    assert s.provider.calls == [] and s.screens.shown == []


@pytest.mark.parametrize("text,why", [
    ("hello littlebot", "chit-chat"), ("ooh aah", "chit-chat"), ("stop talking", "stop"),
    ("what am I holding?", "look"), ("turn the volume up", "control"), ("let's play a game", "control"),
    ("can you sing a song", "song"), ("can you say moo", "talking to the baby"), ("she's tired", "no question"),
])
def test_the_pre_filter(text, why):
    assert prefilter(text) == why


@pytest.mark.parametrize("text", [
    "what's the weather tomorrow", "What does serendipity mean?", "How do I make pancakes?",
    "set a timer for 10 minutes", "what's 15 percent of 80", "what time is it in Tokyo", "convert 5 miles to km",
    "tell me about dinosaurs", "Is it going to rain?",
])
def test_questions_go_to_the_model(text):
    assert prefilter(text) is None


def test_with_a_card_up_follow_ups_go_to_the_model():
    assert prefilter("and on saturday?") == "no question"
    assert prefilter("and on saturday?", card_up=True) is None
    assert prefilter("thanks", card_up=True) == "chit-chat"


@pytest.mark.parametrize("answer,why", [
    ("Sure! Here's a card.", "no JSON"),
    ('{"do": "show", "spec": {"card": "spaceship", "title": "Zoom"}}', "unknown card"),
    ('{"do": "show", "spec": {"card": "recipe", "title": "Pancakes"}}', "missing steps"),
    ('{"do": "show", "spec": "weather"}', "not an object"),
    ('{"do": "command", "action": "next"}', "a command with no card up"),
    ('{"do": "close"}', "nothing to close"),
    ('{"do": 42}', "unknown answer"),
    ("[1, 2, 3]", "no JSON"),
])
async def test_garbage_answers_show_nothing(make, answer, why):
    s = make(answer)
    await s.heard("What's the weather like tomorrow?")
    assert s.screens.shown == [] and s.notes == []
    assert why in s.director.history[-1]["detail"]


async def test_an_unknown_action_for_the_card_up_does_nothing(make):
    s = make(WEATHER, {"do": "command", "action": "explode", "args": {}})
    await s.heard("What's the weather like tomorrow?")
    await s.heard("and what about the day after?")
    assert s.screens.commands == [] and "no such action" in s.director.history[-1]["detail"]


def test_parse_decision_tolerates_fences_and_words_around_it():
    assert parse_decision('```json\n{"do": "close"}\n```') == {"do": "close"}
    assert parse_decision('Here: {"do": "none"} done') == {"do": "none"}
    assert parse_decision("{not json}") is None and parse_decision("") is None


def test_parse_decision_forgives_what_fast_models_do():
    assert parse_decision('{"do": "show", "spec": {"card": "timer", "minutes": 5,},}') == \
        {"do": "show", "spec": {"card": "timer", "minutes": 5}}  # trailing commas
    assert parse_decision('{"do": "close"}\n{"do": "none"}') == {"do": "close"}  # two objects
    assert parse_decision('4! {"do": "show", "spec": {"card": "maths"}} and {x}') == \
        {"do": "show", "spec": {"card": "maths"}}
    assert parse_decision("none") == {"do": "none"} and parse_decision('"close".') == {"do": "close"}
    assert parse_decision("2 + 2 is 4") is None


async def test_an_answer_without_json_is_asked_for_once_more(make):
    s = make("Two plus two is four!", WEATHER)
    await s.heard("What's the weather like tomorrow?")
    assert s.screens.shown == [({"card": "weather", "day": 1}, "voice")]
    assert len(s.provider.calls) == 2 and director_mod.RETRY_NOTE in s.provider.calls[1]["messages"][0]["content"]


async def test_a_new_question_may_replace_the_card_up(make):
    s = make(WEATHER, {"do": "show", "spec": {"card": "recipe", "title": "Pancakes", "steps": ["Mix", "Fry"]}})
    await s.heard("What's the weather like tomorrow?")
    await s.heard("How do I make pancakes?")
    assert [spec["card"] for spec, _ in s.screens.shown] == ["weather", "recipe"]
    assert "Never leave a card about something else up" in s.provider.calls[-1]["system"]


def test_specs_are_trimmed_to_the_cards_fields_types_and_sizes():
    spec, why = clean_spec({"card": "Recipe", "title": "P" * 500, "serves": "4", "evil": "<script>",
                            "steps": ["  Mix\x00 the   flour  ", 7, {"nested": 1}, None, *[f"step {i}" for i in range(30)]]},
                           TYPES)
    assert why == "" and spec is not None
    assert spec["card"] == "recipe" and "evil" not in spec
    assert len(spec["title"]) == 60 and spec["title"].endswith("…")
    assert spec["serves"] == 4
    assert spec["steps"][:2] == ["Mix the flour", "7"] and len(spec["steps"]) == 10
    clamped, _ = clean_spec({"card": "timer", "minutes": 9999, "label": True}, TYPES)
    assert clamped == {"card": "timer", "minutes": 180}
    assert clean_spec({"card": "timer", "minutes": "soon"}, TYPES) == (None, "missing minutes")
    assert clean_spec({"card": "timer", "minutes": float("nan")}, TYPES)[0] is None
    assert clean_spec({"card": "weather", "day": 1.5}, TYPES)[0] == {"card": "weather"}  # not an integer: dropped
    assert clean_args({"offset": 1, "a": "b", "c": [1], "d": 2, "e": 3}) == {"offset": 1, "a": "b", "d": 2}
    assert clean_args("tomorrow") is None and clean_args(None) == {}


async def test_card_text_goes_through_the_safety_rules(make):
    s = make({"do": "show", "spec": {"card": "recipe", "title": "Cookies",
                                     "steps": ["Mix the butter", "This is our little secret, don't tell mommy"]}})
    await s.heard("How do I make cookies with the baby?")
    assert s.screens.shown == [] and s.notes == []
    assert "blocked by the safety rules (secrecy)" in s.director.history[-1]["detail"]
    ok, _ = clean_spec({"card": "recipe", "title": "Soup", "steps": ["Chop the onion with a sharp knife",
                                                                       "Preheat the oven to 180C"]}, TYPES)
    assert ok is not None  # a grown-up's recipe is fine
    assert clean_spec({"card": "timer", "minutes": 5, "label": "Climb on the bookcase"}, TYPES)[0] is None
    assert clean_args({"note": "keep it a secret from mommy"}) is None


async def test_a_slow_answer_times_out_and_shows_nothing(make):
    s = make(WEATHER, delay=0.5, timeout=0.1)
    await s.heard("What's the weather like tomorrow?")
    assert s.screens.shown == [] and s.director.history[-1]["detail"] == "no answer in time"


async def test_a_failed_request_shows_nothing(make):
    s = make(WEATHER, error=ProviderError("nope"))
    await s.heard("What's the weather like tomorrow?")
    assert s.screens.shown == [] and "the request failed" in s.director.history[-1]["detail"]


async def test_an_answer_for_an_older_turn_is_dropped(make):
    s = make(WEATHER, delay=0.1)
    s.director.heard("What's the weather like tomorrow?")
    await asyncio.sleep(0.02)
    s.director.heard("ok thanks")  # a newer turn: the first answer is out of date when it comes
    await s.settle()
    assert s.screens.shown == [] and s.director.history[-1]["what"] == "dropped"


async def test_a_busy_screen_stays_silent(make):
    s = make(WEATHER)
    s.screens.busy = True  # a game or a video owns the screen
    await s.heard("What's the weather like tomorrow?")
    assert len(s.screens.shown) == 1 and s.notes == []
    assert s.director.history[-1]["detail"] == "not shown (busy)"


async def test_switched_off_no_screens_cloud_off_or_a_game_means_no_request(make, ctx):
    s = make(WEATHER)
    set_cards_enabled(ctx, False)
    await s.heard("What's the weather like tomorrow?")
    set_cards_enabled(ctx, True)

    class Games:
        active = True

    ctx.games = Games()
    await s.heard("What's the weather like tomorrow?")
    ctx.games = None
    ctx.privacy.set(cloud_enabled=False)
    await s.heard("What's the weather like tomorrow?")
    assert s.provider.calls == [] and s.screens.shown == []
    s.screens.card = FakeCard("recipe", "Pancakes, step 1")
    await s.heard("next step")  # local rules need no cloud
    assert s.screens.commands == [("next", {})]
    ctx.screens = None
    await s.heard("next step")
    assert len(s.screens.commands) == 1 and not s.director.enabled()


# ---------------------------------------------------------------- local rules
async def test_common_requests_skip_the_model(make):
    s = make(WEATHER)
    s.screens.card = FakeCard("recipe", "Pancakes, step 1")
    await s.heard("Okay, next step please.")
    await s.heard("go back")
    await s.heard("What's next?")
    assert s.screens.commands == [("next", {}), ("back", {}), ("next", {})]
    assert s.notes[0] == "The screen now shows: recipe after next {}."
    s.screens.unchanged = True
    await s.heard("next")
    assert len(s.notes) == 3  # nothing changed on the screen: nothing to tell
    await s.heard("Close it, please.")
    assert s.screens.closed and s.notes[-1] == CLOSED_NOTE
    s.screens.card = FakeCard("timer", "Egg timer, 4:12 left")
    await s.heard("stop the timer")
    assert s.screens.commands[-1] == ("stop", {})
    assert s.provider.calls == []
    assert {h["path"] for h in s.director.history} == {"rule"}


@pytest.mark.parametrize("text,kind,expect", [
    ("next", "recipe", ("command", "next")),
    ("ok what's next", "recipe", ("command", "next")),
    ("show me the next step", "recipe", ("command", "next")),
    ("previous step", "recipe", ("command", "back")),
    ("hide that", "weather", ("close", "")),
    ("take that off the screen", "weather", ("close", "")),
    ("show me your face again", "recipe", ("close", "")),
    ("cancel the timer", "timer", ("command", "stop")),
    ("next", "weather", None),  # no such action: the model decides
    ("next time we go to the park", "recipe", None),
    ("close the door", "recipe", None),
    ("next", None, None),
])
def test_local_rules(text, kind, expect):
    actions = TYPES[kind].actions if kind else {}
    got = local_rule(text, kind, actions)
    assert (None if got is None else (got.do, got.action)) == expect


def test_stop_the_timer_closes_a_timer_card_without_a_stop_action():
    got = local_rule("stop the timer", "timer", {})
    assert got is not None and got.do == "close"


# ---------------------------------------------------------------- the games menu
async def test_show_me_the_games_puts_the_menu_up_and_the_voice_lists_them(make):
    s = make(WEATHER)
    await s.heard("What games do you have?")
    assert s.screens.shown == [({"card": "games"}, "voice")] and s.provider.calls == []
    assert s.notes == [GAMES_NOTE.format(what="Rock Paper Scissors, Bubble Pop, Memory Lights")]
    assert "Say the games' names out loud" in s.notes[0]
    await s.heard("show me the games")  # it's up already
    assert len(s.screens.shown) == 1


@pytest.mark.parametrize("text", ["show me the games", "Which games can we play?", "what games", "show your games",
                                  "OK, what games have you got?"])
def test_games_menu_words(text):
    got = local_rule(text, None, {}, kinds=TYPES)
    assert got is not None and got.do == "show" and got.spec == {"card": "games"}


def test_no_games_menu_without_a_games_card_or_for_playing_one():
    assert local_rule("show me the games", None, {}, kinds={"weather": 1}) is None
    assert local_rule("let's play a game", None, {}, kinds=TYPES) is None


# ---------------------------------------------------------------- only for a grown-up
async def test_short_turns_get_no_card_without_voice_recognition(make):
    s = make(WEATHER, {"do": "command", "action": "day", "args": {"offset": 2}})
    s.screens.card = FakeCard("weather", "weather: home")
    await s.heard("and tomorrow?")  # two words: could be anyone
    assert s.provider.calls == []
    assert real_words("ba ba ba") == 0 and real_words("what's the weather") == 3


async def test_a_recognised_grown_up_gets_cards_for_short_words(make, ctx):
    s = make({"do": "command", "action": "day", "args": {"offset": 1}})
    ctx.voices = object()
    s.screens.card = FakeCard("weather", "weather: home")
    ctx.bus.publish("voice.speaker", {"person_id": 1, "name": "Ali", "role": "dad", "is_child": False, "score": 0.9})
    await s.heard("tomorrow?")
    assert s.screens.commands == [("day", {"offset": 1})]


async def test_the_grown_up_may_be_recognised_while_the_model_answers(make, ctx):
    s = make({"do": "command", "action": "day", "args": {"offset": 1}}, delay=0.05)
    ctx.voices = object()
    s.screens.card = FakeCard("weather", "weather: home")
    s.director.heard("tomorrow?")
    ctx.bus.publish("voice.speaker", {"is_child": False, "name": "Ali"})
    await s.settle()
    assert s.screens.commands == [("day", {"offset": 1})]
    s.director._spoke = None
    s.director.heard("and Sunday?")  # nobody recognised: not known to be a grown-up
    await s.settle()
    assert len(s.screens.commands) == 1 and s.director.history[-1]["detail"] == "not known to be a grown-up"


async def test_the_child_gets_no_card(make, ctx):
    s = make(WEATHER, delay=0.05)
    ctx.voices = object()
    ctx.bus.publish("voice.speaker", {"is_child": True, "name": "Amara"})
    await s.heard("what's the weather tomorrow")
    s.screens.card = FakeCard("recipe", "Pancakes, step 1")
    await s.heard("next")
    assert s.provider.calls == [] and s.screens.shown == [] and s.screens.commands == []
    s.director._spoke = None
    s.director.heard("what's the weather tomorrow")  # recognised as the child a moment later
    ctx.bus.publish("voice.speaker", {"is_child": True, "name": "Amara"})
    await s.settle()
    assert s.screens.shown == [] and s.director.history[-1]["detail"] == "the child spoke"


# ---------------------------------------------------------------- cards from the tools, the log, the end
async def test_a_tools_card_is_checked_and_not_shown_twice(make):
    s = make(WEATHER)
    first = await s.director.show_spec({"card": "weather", "place": "Leeds", "evil": 1})
    assert first["ok"] and s.screens.shown == [({"card": "weather", "place": "Leeds"}, "tool")]
    assert s.notes[0].startswith("On your screen now: weather: Leeds.")
    again = await s.director.show_spec({"card": "weather"})
    assert again["already"] and len(s.screens.shown) == 1
    await s.heard("What's the weather like tomorrow?")  # the director's own decision for the same card
    assert len(s.screens.shown) == 1 and "up already" in s.director.history[-1]["detail"]
    paris = await s.director.show_spec({"card": "weather", "place": "Paris"})
    assert paris["ok"] and len(s.screens.shown) == 2
    bad = await s.director.show_spec({"card": "recipe", "title": "x", "steps": ["Keep it our secret from mommy"]})
    assert bad == {"ok": False, "error": "invalid", "detail": "blocked by the safety rules (secrecy)"}


async def test_one_info_line_per_decision_without_the_words(make, caplog):
    s = make(WEATHER)
    with caplog.at_level(logging.INFO, logger="littlebot.screens.director"):
        await s.heard("What's the weather like in Springfield tomorrow?")
        await s.heard("hi")
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 1 and lines[0].startswith("screen: show weather (model, ")
    assert "Springfield" not in caplog.text


async def test_after_the_call_nothing_more_is_decided(make, ctx):
    s = make(WEATHER, delay=0.05)
    s.director.heard("What's the weather like tomorrow?")
    s.director.close()
    await asyncio.sleep(0.1)
    await s.heard("What's the weather like tomorrow?")
    assert s.screens.shown == [] and s.notes == [] and s.director._lines == director_mod.collections.deque(maxlen=6)
    ctx.bus.publish("voice.speaker", {"is_child": True})
    assert s.director._spoke is None  # no longer listening


# ---------------------------------------------------------------- the card follows LittleBot's answer (live calls)
FACT = {"do": "show", "spec": {"card": "recipe", "title": "Pancakes", "steps": ["Mix", "Fry"]}}


async def test_it_waits_for_the_voices_answer_not_its_filler(make):
    s = make(FACT, wait_for_answer=True)
    s.director.heard("How do I make pancakes for breakfast?")
    await asyncio.sleep(0.05)
    s.director.said("Hold on, let me check.")
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.2)
    assert s.provider.calls == []  # "hold on" isn't the answer: no card while it's thinking
    s.director.said("Mix the flour, eggs and milk, then fry them in a hot pan.")
    await s.settle()
    content = s.provider.calls[0]["messages"][-1]["content"]
    assert "LittleBot: Mix the flour, eggs and milk" in content and "being said now" not in content
    assert "take its numbers, names and facts from LittleBot's answer" in s.provider.calls[0]["system"]
    assert s.screens.shown and s.screens.shown[0][0]["card"] == "recipe"


async def test_a_bare_sure_is_not_the_answer(make):
    s = make(FACT, wait_for_answer=True)
    s.director.heard("Can you tell me what the weather's like tomorrow?")
    await asyncio.sleep(0.05)
    s.director.said("Sure.")  # from a real call: the card was made from "Sure." before the forecast came
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.2)
    assert s.provider.calls == []
    s.director.said("Tomorrow in Hamilton, it's cloudy with a high of about 12.")
    await s.settle()
    assert "LittleBot: Tomorrow in Hamilton" in s.provider.calls[0]["messages"][-1]["content"]


@pytest.mark.parametrize("line", ["Sure.", "Sure!", "Okay, looking.", "Looking.", "Mm, sure thing.", "Hmm."])
def test_a_line_that_is_only_sure_or_looking_is_filler(line):
    assert director_mod.FILLER.match(line)


@pytest.mark.parametrize("line", ["Sure! Genghis Khan was a leader a long time ago.",
                                  "Looking. I can't read it clearly from here.", "He was, yeah.", "Yes.", "Okay, three fingers!",
                                  "Surely not", "m" * 40 + "x"])
def test_an_answer_that_starts_with_sure_is_not_filler(line):
    assert not director_mod.FILLER.match(line)


async def test_a_hand_off_answer_is_what_the_card_is_made_from(make):
    s = make(FACT, wait_for_answer=True)
    s.director.heard("What's the top-grossing movie of all time?")
    s.director.handoff_started()
    s.director.said("Good question, it's interesting, give me a second to find out about that one.")
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.2)
    assert s.provider.calls == []  # the hand-off is open: the voice's words aren't the answer
    s.director.handoff_answer("Avatar, with about 2.9 billion dollars.")
    await s.settle()
    s.director.handoff_done()
    content = s.provider.calls[0]["messages"][-1]["content"]
    assert content.splitlines()[-3] == "LittleBot's answer, being said now: Avatar, with about 2.9 billion dollars."


async def test_a_hand_off_without_an_answer_falls_back_to_what_the_voice_says(make):
    s = make(FACT, wait_for_answer=True)
    s.director.heard("How do I make pancakes for breakfast?")
    s.director.handoff_started()
    s.director.said("You mix flour, eggs and milk and fry them.")
    await asyncio.sleep(director_mod.SAID_GRACE_S + 0.2)
    assert s.provider.calls == []
    s.director.handoff_done()
    await s.settle()
    assert len(s.provider.calls) == 1


async def test_no_second_card_when_the_hand_off_put_one_up(make):
    s = make(FACT, wait_for_answer=True)
    s.director.heard("How do I make pancakes for breakfast?")
    s.director.handoff_started()
    assert (await s.director.show_spec({"card": "recipe", "title": "Pancakes", "steps": ["Mix", "Fry"]}))["ok"]
    s.director.handoff_answer("Mix flour, eggs and milk, then fry.")
    await s.settle()
    assert s.provider.calls == [] and len(s.screens.shown) == 1


async def test_no_answer_in_time_means_no_card(make, caplog):
    s = make(FACT, wait_for_answer=True, answer_wait_s=0.3)
    with caplog.at_level(logging.INFO, logger="littlebot.screens.director"):
        s.director.heard("How do I make pancakes for breakfast?")
        await s.settle()
    assert s.provider.calls == [] and s.screens.shown == []
    assert "answer didn't come in time" in caplog.text


def test_an_unknown_card_is_named_in_the_log_reason():
    assert clean_spec({"card": "Movie<b>"}, TYPES) == (None, "unknown card 'movieb'")
