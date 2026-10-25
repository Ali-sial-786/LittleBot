"""Live voice with a decision model (littlebot/decide): hand-off routing, the voice talking to the grown-up as
the baby (naming), and "could someone be in danger?" (concern), on and in shadow."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

pytest.importorskip("aiortc")

from test_live import connect, hear, live, notes, wait_for  # noqa: E402, F401 - live is a fixture
from test_live_tools import ToolProvider, answers, close, delegate, make_live  # noqa: E402, F401

from littlebot.decide import Answer, get_decider  # noqa: E402
from littlebot.decide.base import Choice  # noqa: E402
from littlebot.testing import decision_answers, use_decision_model  # noqa: E402

pytestmark = pytest.mark.timeout(60)


def only(feature_question: str, value: Any, confidence: float = 0.9) -> Any:
    """Answers only the question called ``feature_question`` (choice: ``value``; yes/no: ``value`` is P(yes))."""
    def answer(state: Any, questions: dict[str, Any]) -> dict[str, Answer]:
        q = questions.get(feature_question)
        if q is None:
            return {}
        if isinstance(q, Choice):
            return {feature_question: Answer("choice", value, confidence)}
        return {feature_question: Answer("yesno", float(value), abs(2 * float(value) - 1))}
    return answer


# ---------------------------------------------------------------- routing
async def test_on_a_hand_off_the_word_lists_take_for_a_face_goes_to_the_tools(ctx, make_live):
    provider = ToolProvider(text="It's sunny, and here's a smile!")
    svc, server, _audio = await make_live(provider)
    try:
        model = use_decision_model(ctx, only("route", "robot"), modes={"routing": "on"})
        delegate(server, "r1", "wink and tell me a joke")
        await wait_for(lambda: answers(server, "r1"))
        assert provider.calls  # the text model answered it (with its tools), not just a face
        assert model.asked[0][0]["handed_to_the_robot"] == "wink and tell me a joke"
        assert get_decider(ctx).status()["features"]["routing"]["used"] == 1
    finally:
        await close(svc, server)


async def test_shadow_routing_only_compares(ctx, make_live, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.decide")
    provider = ToolProvider()
    svc, server, _audio = await make_live(provider)
    try:
        use_decision_model(ctx, only("route", "robot"), modes={"routing": "shadow"})
        delegate(server, "r2", "face: happy")
        await wait_for(lambda: answers(server, "r2"))
        assert answers(server, "r2")[0].startswith("Done") and provider.calls == []  # the face, as before
        await wait_for(lambda: "routing (shadow): LittleBot's way face, jev robot, different" in caplog.text)
        line = next(r.getMessage() for r in caplog.records if "routing (shadow)" in r.getMessage())
        assert "| handed_to_the_robot: face: happy |" in line and "confidence: 0.90" in line  # what it was shown
        assert get_decider(ctx).sensors is not None  # the call adds who face / voice recognition found
    finally:
        await close(svc, server)
    assert get_decider(ctx).sensors is None


async def test_the_log_gets_the_last_thing_heard_and_who_was_recognised(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    hear(server, "What is the weather like today?")
    call = svc._call
    await wait_for(lambda: any(c["who"] == "heard" for c in call._captions))
    call._faces = type("F", (), {"summary": lambda self: "Ali (dad)"})()
    call._voices = type("V", (), {"summary": lambda self: "Ali (dad), 0.90"})()
    try:
        assert call._sensed() == {"last_heard": "What is the weather like today?", "camera_sees": "Ali (dad)",
                                  "voice_is": "Ali (dad), 0.90"}
    finally:
        call._faces = call._voices = None


async def test_after_a_decision_the_log_gets_what_littlebot_said_next(live, ctx, caplog):
    caplog.set_level(logging.INFO, logger="littlebot.decide")
    svc, server, _audio, _provider, _alerts = live
    use_decision_model(ctx, decision_answers("x", 0.05), modes={"concern": "shadow", "naming": "off"})
    await connect(svc, server)
    hear(server, "she climbed up on the kitchen counter")
    await wait_for(lambda: "concern (shadow)" in caplog.text)
    for line in ("Hold on, one moment!", "Please help her down, counters are high!", "Anything else?"):
        server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": line}})
    await wait_for(lambda: "counters are high" in caplog.text)
    await asyncio.sleep(0.1)
    said = [r.getMessage() for r in caplog.records if "LittleBot then said" in r.getMessage()]
    assert said == ["decide: LittleBot then said (after concern): Hold on, one moment!",  # "hold on" keeps waiting
                    "decide: LittleBot then said (after concern): Please help her down, counters are high!"]


async def test_on_but_a_face_it_cant_name_stays_the_word_lists_way(ctx, make_live):
    provider = ToolProvider(text="Sure.")
    svc, server, _audio = await make_live(provider)
    try:
        use_decision_model(ctx, only("route", "face"), modes={"routing": "on"})
        delegate(server, "r3", "what's the capital of France")
        await wait_for(lambda: answers(server, "r3"))
        assert provider.calls  # no face is named in it: the text model, as the word lists said
    finally:
        await close(svc, server)


# ---------------------------------------------------------------- naming
async def test_on_talking_to_the_grown_up_as_the_baby_is_put_right(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    ctx.settings.set("profile", {"nickname": "Amara", "age_months": 10})
    model = use_decision_model(ctx, only("as_baby", 0.95), modes={"naming": "on", "concern": "off"})
    await connect(svc, server)
    hear(server, "What is the weather like today?")
    server.send({"type": "turn.done", "turn": {"role": "assistant",
                                               "transcript": "Ooh, good job sweetie, it's sunny today!"}})
    await wait_for(lambda: any(n.startswith("Careful: you just talked") for n in notes(server)))
    state = model.asked[0][0]
    assert state["the_baby"] == "Amara, too young to talk" and "sweetie" in state["LittleBot_replied"]


async def test_shadow_naming_says_nothing(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    ctx.settings.set("profile", {"nickname": "Amara", "age_months": 10})
    use_decision_model(ctx, only("as_baby", 0.95), modes={"naming": "shadow", "concern": "off"})
    await connect(svc, server)
    hear(server, "What is the weather like today?")
    server.send({"type": "turn.done", "turn": {"role": "assistant",
                                               "transcript": "Ooh, good job sweetie, it's sunny today!"}})
    await wait_for(lambda: get_decider(ctx).status()["features"]["naming"]["differed"] == 1)
    await asyncio.sleep(0.1)
    assert not any(n.startswith("Careful") for n in notes(server))


# ---------------------------------------------------------------- concern
@pytest.mark.parametrize(("mode", "alerted"), [("on", True), ("shadow", False)])
async def test_concern_only_adds_an_alert_when_on(live, ctx, mode, alerted):
    svc, server, _audio, _provider, alerts = live
    use_decision_model(ctx, decision_answers("x", 0.97), modes={"concern": mode, "naming": "off"})
    await connect(svc, server)
    hear(server, "she's gone very quiet and her lips look blue")
    await wait_for(lambda: get_decider(ctx).status()["features"]["concern"]["asked"] == 1)
    await asyncio.sleep(0.2)
    assert any(a.get("detail") == "decider" for a in alerts) is alerted


# ---------------------------------------------------------------- the voice saying the person's part
@pytest.mark.parametrize(("line", "hit"), [
    ("Hi LittleBot, it's Ali, can you look up the weather?", True),  # from a real call
    ("Hey Little Bot, what's the weather?", True),
    ("Okay, little-bot, can you play a song?", True),
    ("Hi, LittleBot here! What shall we do?", False),
    ("Hello! LittleBot's here.", False),
    ("Hi Ali! Want to hear about the weather?", False),
    ("I'm LittleBot, your robot friend.", False),
])
def test_greeting_littlebot_by_name_is_the_persons_part(line, hit):
    from littlebot.chatgpt.live import _greets_robot

    assert _greets_robot(line, "LittleBot") is hit


async def test_the_voice_is_told_when_it_says_the_persons_part(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    for _ in range(3):
        server.send({"type": "turn.done", "turn": {"role": "assistant",
                                                   "transcript": "Hi LittleBot, it's Ali, can you look up the weather?"}})
    await wait_for(lambda: any(n.startswith("You just spoke as if you were the person") for n in notes(server)))
    await asyncio.sleep(0.2)
    told = [n for n in notes(server) if n.startswith("You just spoke as if")]
    assert len(told) == 2 and '("Hi LittleBot, it\'s Ali, can you look up...")' in told[0]  # at most 2
