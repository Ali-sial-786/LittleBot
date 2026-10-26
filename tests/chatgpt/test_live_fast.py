"""Live voice: faster hand-offs. The answer's first sentence goes to the voice while the rest is written, simple
questions get a lighter request, and the weather, the time, the volume and timers need no text model at all
(littlebot/chatgpt/quick.py)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

pytest.importorskip("aiortc")

from test_live import wait_for  # noqa: E402
from test_live_tools import answers, close, delegate, make_live, notes  # noqa: E402, F401 - make_live is a fixture

import littlebot.chatgpt.live as live_mod  # noqa: E402
from littlebot.brain.providers.base import BaseProvider, ProviderReply  # noqa: E402
from littlebot.chatgpt import quick  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class StreamProvider(BaseProvider):
    """The text model, streaming its answer: ("text", words), ("sleep", s), ("tool", name, args) steps."""

    name = "chatgpt"
    is_cloud = True
    model = "fake"
    supports_tools = True

    def __init__(self, steps: list[tuple[Any, ...]]) -> None:
        self.steps = steps
        self.calls: list[dict[str, Any]] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: list[dict[str, Any]] | None = None, run_tool: Any = None, max_tool_rounds: int = 6,
                    images: Any = None, on_delta: Any = None, effort: str | None = None,
                    final_tools: Any = ()) -> ProviderReply:
        self.calls.append({"tools": [t["name"] for t in tools or []], "effort": effort,
                           "final_tools": set(final_tools), "system": system})
        out: list[str] = []
        for step in self.steps:
            if step[0] == "text":
                out.append(step[1])
                if on_delta is not None:
                    on_delta(step[1])
            elif step[0] == "sleep":
                await asyncio.sleep(step[1])
            elif step[0] == "tool":
                await run_tool(step[1], json.dumps(step[2]))
                out = []  # the answer is what comes after the last tool call
        return ProviderReply(text="".join(out), model="fake")


async def test_the_first_sentence_is_the_answer_and_the_rest_follows(ctx, make_live, monkeypatch):
    known: list[str] = []
    monkeypatch.setattr(live_mod.LiveCall, "_answer_known", lambda self, text: known.append(text))
    provider = StreamProvider([("text", "Giraffes are about five metres tall. "), ("sleep", 1.0),
                               ("text", "They eat leaves from the tops of trees.")])
    svc, server, _audio = await make_live(provider)
    try:
        t0 = time.monotonic()
        delegate(server, "g1", "How tall is a giraffe?")
        await wait_for(lambda: answers(server, "g1"))
        assert time.monotonic() - t0 < 0.8  # not the whole second the rest took
        assert answers(server, "g1") == ["Giraffes are about five metres tall."]
        assert known == ["Giraffes are about five metres tall."]  # the screen's card is decided now, not after
        more = live_mod.MORE_ANSWER.format(rest="They eat leaves from the tops of trees.")
        await wait_for(lambda: more in notes(server))
        assert known[-1] == "Giraffes are about five metres tall. They eat leaves from the tops of trees."
        # a simple question: a lighter request, no web search; show_on_screen needs no second request
        call = provider.calls[0]
        assert call["effort"] == live_mod.LIGHT_EFFORT and "web_search" not in call["tools"]
        assert call["final_tools"] == {"show_on_screen"} and "write your spoken answer first" in call["system"]
    finally:
        await close(svc, server)


async def test_words_before_a_tool_call_are_not_the_answer(ctx, make_live):
    provider = StreamProvider([("text", "Let me check that for you. "), ("text", "One moment while I look. "),
                               ("tool", "get_volume", {}),
                               ("text", "Your volume is at fifty percent. "), ("sleep", 1.0), ("text", "That's half.")])
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "n1", "What's the latest news about giraffes?")  # news: the full request
        await wait_for(lambda: answers(server, "n1"))
        assert answers(server, "n1") == ["Your volume is at fifty percent."]
        await wait_for(lambda: live_mod.MORE_ANSWER.format(rest="That's half.") in notes(server))
        assert provider.calls[0]["effort"] is None and "web_search" in provider.calls[0]["tools"]
    finally:
        await close(svc, server)


async def test_an_answer_that_changed_after_its_first_sentence_is_told_whole(ctx, make_live):
    provider = StreamProvider([("text", "Paris is lovely in the spring. "), ("sleep", 0.6),
                               ("tool", "get_volume", {}), ("text", "The Eiffel Tower is 330 metres tall.")])
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "e1", "How tall is the Eiffel Tower?")
        await wait_for(lambda: answers(server, "e1"))
        assert answers(server, "e1") == ["Paris is lovely in the spring."]
        whole = live_mod.LATE_ANSWER.format(answer="The Eiffel Tower is 330 metres tall.")
        await wait_for(lambda: whole in notes(server))
        assert not any(n.startswith("More of the answer") for n in notes(server))
    finally:
        await close(svc, server)


async def test_a_quick_whole_answer_goes_as_it_is(ctx, make_live):
    provider = StreamProvider([("text", "A spider has eight legs. "), ("text", "Insects have six.")])
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "s1", "How many legs does a spider have?")
        await wait_for(lambda: answers(server, "s1"))
        await asyncio.sleep(0.2)
        assert answers(server, "s1") == ["A spider has eight legs. Insects have six."]
        assert not any(n.startswith("More of the answer") for n in notes(server))
    finally:
        await close(svc, server)


async def test_a_timer_needs_no_text_model(ctx, make_live, monkeypatch):
    shown: list[dict[str, Any]] = []

    class Box:
        ctx = None
        calls: list[str] = []

        def screen_cards(self) -> bool:
            return True

        async def run(self, name: str, args: dict[str, Any]) -> str:
            shown.append({"tool": name, **args})
            return "ok: on the screen now: a timer"

    monkeypatch.setattr(live_mod.LiveCall, "_toolbox", lambda self: Box())
    provider = StreamProvider([("text", "Okay!")])
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "t1", "Set a timer for 5 minutes for the pasta")
        await wait_for(lambda: answers(server, "t1"))
        assert answers(server, "t1")[0].startswith("Done: a timer for 5 minutes (for the pasta) is counting down")
        assert shown == [{"tool": "show_on_screen", "card": "timer", "fields": {"seconds": 300, "label": "pasta"}}]
        assert provider.calls == []
    finally:
        await close(svc, server)


@pytest.mark.parametrize(("question", "light"), [
    ("How tall is a giraffe?", True),
    ("What's the capital of Peru?", True),
    ("Tell me some facts about Kublai Khan", True),  # from a real call
    ("Who won the hockey game last night?", False),
    ("What's the news today?", False),
    ("Is the zoo open on Sundays?", False),
    ("How old is Taylor Swift?", False),
    ("Can you look up the best pancake recipe online?", False),
    ("Why is the sky blue and why do sunsets look orange and red in the evening sometimes?", False),  # long
])
def test_which_questions_are_simple(question, light):
    assert live_mod._light_question(question) is light


@pytest.mark.parametrize(("question", "what", "args"), [
    ("What's the weather like today?", "weather", {"place": None, "days": 2}),
    ("What's the weather in Toronto tomorrow?", "weather", {"place": "Toronto", "days": 2, "view": "day",
                                                                    "day": "tomorrow"}),
    ("Is it going to be sunny in New York City this week?", "weather", {"place": "New York City", "days": 7,
                                                                                   "view": "week"}),
    ("Should I take an umbrella?", "weather", {"place": None, "days": 2}),
    ("weather in paris", None, None),  # a place it can't be sure of: the text model
    ("Why does it rain?", None, None),
    ("What time is it?", "time", {}),
    ("What's the date today?", "time", {}),
    ("What time is it in Tokyo?", None, None),
    ("What time does the zoo open?", None, None),
    ("How long until bedtime?", None, None),
    ("Turn the volume up", "volume", {"action": "up", "step": 10}),
    ("Turn it up a lot", "volume", {"action": "up", "step": 20}),
    ("can you turn the video down", "volume", {"action": "down", "step": 10}),
    ("Set the volume to 40 percent", "volume", {"action": "set", "percent": 40}),
    ("What's the volume?", "volume", {"action": "get"}),
    ("Don't turn it up", None, None),
    ("Play baby shark louder", None, None),  # a video: the text model plays it
    ("Set a timer for 5 minutes", "timer", {"seconds": 300}),
    ("Set a 3-minute egg timer", "timer", {"seconds": 180, "label": "egg"}),
    ("Timer for an hour and a half", "timer", {"seconds": 5400}),
    ("set a half an hour timer", "timer", {"seconds": 1800}),
    ("set a timer for twenty-five minutes", "timer", {"seconds": 1500}),
    ("set a timer for 2 minutes 30 seconds for the pasta", "timer", {"seconds": 150, "label": "pasta"}),
    ("cancel the timer", None, None),
    ("How much time is left on the timer?", None, None),
    ("Show the picture in full screen", "picture", {"view": "fill"}),  # from a real call
    ("zoom in on the picture", "picture", {"view": "in"}),
    ("Make the text bigger", None, None),
])
def test_what_littlebot_answers_without_the_text_model(question, what, args):
    asked = quick.match(question)
    assert (asked.what, asked.args) == (what, args) if what else asked is None, asked


def test_a_weeks_weather_fits_in_one_answer():
    day = {"day": "Monday", "date": "Monday 26 October", "conditions": "light rain showers", "high": 12, "low": 4,
           "chance_of_rain_or_snow_percent": 80, "wind_max": 25, "gusts_max": 40, "uv_index_max": 3.0,
           "sunrise": "07:54", "sunset": "18:36"}
    report = {"place": "Toronto, Ontario", "local_time": "09:00", "units": {"temperature": "°C", "wind": "km/h"},
              "now": {"temperature": 8, "feels_like": 5, "conditions": "overcast", "humidity_percent": 82, "wind": 15,
                      "gusts": 30}, "forecast": [day] * 7, "location_from": "home, set on the dashboard"}
    data = json.dumps(quick._compact(report), ensure_ascii=False, separators=(",", ":"))
    from littlebot.chatgpt import wire

    assert len(data) + 300 < wire.MAX_RESULT_CHARS and "uv_index_max" not in data
