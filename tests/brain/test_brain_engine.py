"""ConversationEngine: provider gating, failures, state/lease handling, STOP ALL, privacy guarantees."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from brain_helpers import (
    ANTHROPIC_KEY, OPENAI_KEY, FakeAPI, anthropic_response, cloud_context, error_response, openai_response,
)

from littlebot.brain import create_service
from littlebot.brain.conversation import ERROR_FALLBACKS, ConversationEngine
from littlebot.brain.providers import BaseProvider, ProviderReply
from littlebot.core.arbiter import Priority
from littlebot.core.events import Topics, collect
from littlebot.core.interfaces import BrainAPI
from littlebot.core.service import Service
from littlebot.core.state import RobotState
from littlebot.safety.content import GROWNUP_FALLBACK, SAFE_FALLBACKS, SHORT_SAFE_FALLBACKS, word_count
from littlebot.testing import FakeAudio, make_test_context


def _route(req: httpx.Request) -> httpx.Response:
    return openai_response() if "chat/completions" in str(req.url) else anthropic_response()


# ------------------------------------------------------------------- basics
async def test_create_service_implements_brain_api_and_service(make_brain):
    ctx = make_test_context()
    brain = create_service(ctx)
    assert isinstance(brain, ConversationEngine) and isinstance(brain, Service) and isinstance(brain, BrainAPI)
    await brain.start()
    try:
        assert brain.health()["status"] == "running"
        st = brain.status()
        for key in ("provider", "active_provider", "cloud_allowed", "reason", "usage", "last_error", "busy", "models"):
            assert key in st
        for key in ("requests_hour", "requests_today", "cost_today_usd", "budget_usd"):
            assert key in st["usage"]
        assert st["models"]["anthropic"] == "claude-haiku-5-5"
        names = {p["name"] for p in brain.providers()}
        assert names == {"mock", "anthropic", "openai", "chatgpt"}
        assert all({"name", "configured", "available", "model"} <= set(p) for p in brain.providers())
    finally:
        await brain.stop()
    assert brain.health()["status"] == "stopped"


# ------------------------------------------------------- provider selection
MATRIX = [
    # (overrides for cloud_context, expected active provider, reason fragment)
    ({}, "anthropic", "cloud AI active"),
    ({"provider": "openai"}, "openai", "cloud AI active"),
    ({"enabled": False}, "mock", "mock: cloud AI disabled by parent"),
    ({"cloud": False}, "mock", "mock: cloud access disabled in privacy settings"),
    ({"mode": "offline"}, "mock", "mock: offline mode"),
    # any age once the parent switches cloud AI on (a grown-up may be the one asking)
    ({"age_months": 18}, "anthropic", "cloud AI active"),
    ({"age_months": 10}, "anthropic", "cloud AI active"),
    ({"age_months": None}, "anthropic", "cloud AI active"),
    ({"key": False}, "mock", "mock: no API key"),
    ({"provider": "mock"}, "mock", "mock: mock provider selected"),
    ({"provider": "skynet"}, "mock", "mock: unknown provider"),
    # ...unless the config sets a minimum age
    ({"age_months": 30, "ai": {"min_age_months_for_cloud": 36}}, "mock", "mock: child under 36 months"),
    ({"age_months": 10, "ai": {"min_age_months_for_cloud": 24}}, "mock", "mock: child under 24 months"),
    ({"age_months": None, "ai": {"min_age_months_for_cloud": 24}}, "mock", "mock: child age not set"),
]


@pytest.mark.parametrize(("overrides", "active", "reason"), MATRIX)
async def test_provider_selection_matrix(make_brain, overrides, active, reason):
    ctx = cloud_context(**overrides)
    api = FakeAPI(_route)
    brain = await make_brain(ctx, api)
    st = brain.status()
    assert st["active_provider"] == active and reason in st["reason"]
    assert st["cloud_allowed"] == (active != "mock")
    result = await brain.handle_utterance("what does the cow say")
    assert result["provider"] == active and result["error"] is None and result["reply"]
    assert api.calls == (1 if active != "mock" else 0)  # gated providers never touch the network


@pytest.mark.parametrize(("setting", "value", "reason"), [
    ("ai.daily_budget_usd", 0.0, "daily budget reached"),
    ("ai.max_requests_per_hour", 0, "hourly request limit reached"),
    ("ai.max_requests_per_day", 0, "daily request limit reached"),
])
async def test_spending_limits_fall_back_to_mock(make_brain, setting, value, reason):
    ctx = cloud_context()
    ctx.settings.set(setting, value)
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    assert brain.status()["reason"] == f"mock: {reason}"
    result = await brain.handle_utterance("hello")
    assert result["provider"] == "mock" and api.calls == 0


async def test_budget_exhausted_after_expensive_reply(make_brain):
    ctx = cloud_context()
    ctx.settings.set("ai.daily_budget_usd", 0.001)
    api = FakeAPI(lambda req: anthropic_response(input_tokens=1_000_000))  # ~$0.10 on Haiku 5.5
    brain = await make_brain(ctx, api)
    assert (await brain.handle_utterance("hello"))["provider"] == "anthropic"
    st = brain.status()
    assert st["usage"]["cost_today_usd"] >= 0.1 and st["reason"] == "mock: daily budget reached"
    assert (await brain.handle_utterance("hello again"))["provider"] == "mock"
    assert api.calls == 1


async def test_hourly_limit_counts_cloud_requests(make_brain):
    ctx = cloud_context()
    ctx.settings.set("ai.max_requests_per_hour", 2)
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    providers = [(await brain.handle_utterance(f"hi {i}"))["provider"] for i in range(3)]
    assert providers == ["anthropic", "anthropic", "mock"]
    assert brain.status()["usage"]["requests_hour"] == 2


async def test_an_infants_family_gets_cloud_once_the_parent_enables_it(make_brain):
    ctx = cloud_context(age_months=10)
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    assert (await brain.handle_utterance("what's the weather like today"))["provider"] == "anthropic"
    assert "a baby about 10 months old" in api.body()["system"] and api.calls == 1


async def test_a_configured_minimum_age_keeps_an_infant_on_scripted_replies(make_brain):
    ctx = cloud_context(age_months=10, ai={"min_age_months_for_cloud": 24})
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    for text in ("tell me a story", "why is the sky blue", "what is a volcano", "sing"):
        r = await brain.handle_utterance(text)
        assert r["provider"] == "mock" and word_count(r["reply"]) <= 8
    assert api.calls == 0
    assert (await brain.test_provider("anthropic"))["ok"]  # parent key test is not a child conversation
    assert api.calls == 1


# --------------------------------------------------------- cloud happy path
async def test_cloud_turn_end_to_end(make_brain):
    ctx = cloud_context(age_months=40)
    events = collect(ctx.bus, [Topics.BRAIN_REPLY, Topics.STATE_CHANGED])
    api = FakeAPI(lambda req: anthropic_response("**Moo!** 🐄 The cow says moo. Can you moo?"))
    brain = await make_brain(ctx, api)
    r = await brain.handle_utterance("what does the cow say")
    assert r == {**r, "reply": "Moo! The cow says moo. Can you moo?", "provider": "anthropic", "filtered": False,
                 "error": None, "spoken": True}
    assert ctx.audio.spoken == [r["reply"]]
    body = api.body()
    assert body["messages"] == [{"role": "user", "content": "what does the cow say"}]
    assert "3–5 years" in body["system"] and "60 words" in body["system"]
    replies = [e.data for e in events if e.topic == Topics.BRAIN_REPLY]
    assert replies == [{"text": r["reply"], "provider": "anthropic", "filtered": False}]
    assert [e.get("new") for e in events if e.topic == Topics.STATE_CHANGED] == ["THINKING", "SPEAKING", "IDLE"]
    usage = brain.status()["usage"]
    assert usage["requests_today"] == 1 and usage["cost_today_usd"] > 0
    exprs = [a[0] for a, _k in ctx.face.called("set_expression")]
    assert exprs[0] == "thinking" and exprs[-1] == "curious"


async def test_cloud_reply_is_capped_at_the_parents_max_words(make_brain):
    ctx = cloud_context(age_months=30)
    long = "Cows are big animals. " * 30
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response(long)))
    r = await brain.handle_utterance("tell me about cows")
    assert word_count(r["reply"]) == 60 and "truncated" in r["checks"]  # ai.max_words, at any age
    ctx.settings.set("ai.max_words", 15)
    r = await brain.handle_utterance("tell me about cows")
    assert word_count(r["reply"]) <= 15 and "truncated" in r["checks"]


@pytest.mark.parametrize("bad", ["This is our little secret!", "What's your address?", "Let's play with matches!",
                                 "Don't tell your mommy, okay?", "Let's climb the bookcase!"])
async def test_unsafe_cloud_reply_is_filtered(make_brain, bad):
    """With every rule group on (ai.relaxed_rules: [])."""
    ctx = cloud_context()
    ctx.config.ai.relaxed_rules = []
    events = collect(ctx.bus, [Topics.BRAIN_REPLY])
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response(bad)))
    r = await brain.handle_utterance("hello")
    assert r["filtered"] is True and r["reply"] in SAFE_FALLBACKS + SHORT_SAFE_FALLBACKS
    assert ctx.audio.spoken == [r["reply"]]
    assert events[-1].data["filtered"] is True and bad not in json.dumps(events[-1].data)
    assert brain.status()["context_turns"] == 0  # unsafe exchange is not remembered


async def test_provider_refusal_is_filtered(make_brain):
    ctx = cloud_context()
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response("", stop_reason="refusal")))
    r = await brain.handle_utterance("hello")
    assert r["filtered"] and r["reply"] and r["error"] is None


# ----------------------------------------------------------------- failures
FAILURES = [
    (lambda req: error_response(401, "authentication_error", "invalid x-api-key"), "auth_error"),
    (lambda req: error_response(429, "rate_limit_error", "slow down"), "rate_limited"),
    (lambda req: error_response(500, "api_error", "boom"), "server_error"),
    (lambda req: error_response(529, "overloaded_error", "busy"), "overloaded"),
    (lambda req: httpx.Response(200, content=b"not json"), "malformed"),
]


def _raise(exc_type):
    def handler(req):
        raise exc_type("failure", request=req)

    return handler


FAILURES += [(_raise(httpx.ConnectError), "network"), (_raise(httpx.ReadTimeout), "timeout")]


@pytest.mark.parametrize(("handler", "kind"), FAILURES)
async def test_api_failures_give_friendly_fallback(make_brain, handler, kind):
    ctx = cloud_context()
    statuses = collect(ctx.bus, [Topics.BRAIN_STATUS])
    brain = await make_brain(ctx, FakeAPI(handler))
    r = await brain.handle_utterance("what does the cow say")
    assert r["error"] == kind and r["provider"] == "anthropic"
    assert r["reply"] == ERROR_FALLBACKS[0] == "Hmm, my thinking cap is sleepy. Let's play instead!"
    assert ctx.audio.spoken == [r["reply"]] and r["expression"] == "confused"
    assert ctx.face.expression == "confused"
    assert brain.status()["last_error"] == kind
    assert statuses[-1].data["status"] == ("auth_error" if kind == "auth_error" else "error")
    assert ctx.state.state == RobotState.IDLE
    assert brain.status()["usage"]["failed_today"] == 1


async def test_invalid_key_flips_to_auth_error_until_settings_change(make_brain):
    ctx = cloud_context()
    api = FakeAPI(lambda req: error_response(401, "authentication_error", "invalid x-api-key"))
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("hello")
    st = brain.status()
    assert st["provider_status"]["anthropic"] == "auth_error"
    assert st["active_provider"] == "mock" and "API key rejected" in st["reason"]
    assert next(p for p in brain.providers() if p["name"] == "anthropic")["status"] == "auth_error"
    r = await brain.handle_utterance("hello again")
    assert r["provider"] == "mock" and api.calls == 1  # no hammering a bad key
    api.respond = lambda req: anthropic_response()
    ctx.settings.set("ai.max_words", 25)  # any AI settings change clears the flag
    assert brain.status()["provider_status"]["anthropic"] == "ok"
    assert (await brain.handle_utterance("hello"))["provider"] == "anthropic"


async def test_engine_timeout_bounds_a_hanging_request(make_brain):
    ctx = cloud_context(ai={"timeout_seconds": 0.2})

    async def hang(req):
        await asyncio.sleep(10)
        return anthropic_response()

    brain = await make_brain(ctx, FakeAPI(hang))
    t0 = time.monotonic()
    r = await brain.handle_utterance("hello")
    assert time.monotonic() - t0 < 2.0
    assert r["error"] == "timeout" and r["reply"] in ERROR_FALLBACKS and ctx.audio.spoken == [r["reply"]]


async def test_unexpected_provider_bug_is_contained(make_brain):
    class Buggy(BaseProvider):
        name, is_cloud, model = "anthropic", True, "buggy"

        async def reply(self, messages, *, system, max_tokens, timeout):
            raise KeyError("oops")

    ctx = cloud_context()
    brain = await make_brain(ctx, providers={"anthropic": Buggy()})
    r = await brain.handle_utterance("hello")
    assert r["error"] == "internal_error" and r["reply"] in ERROR_FALLBACKS


# ------------------------------------------------- state, leases, STOP ALL
async def test_push_to_talk_state_sequence(make_brain):
    ctx = make_test_context()
    events = collect(ctx.bus, [Topics.STATE_CHANGED])
    brain = await make_brain(ctx)
    ctx.audio.inject_utterance("what does the cow say")
    r = await brain.converse_once()
    assert r["heard"] == "what does the cow say" and r["reply"] == "Moo! Moo!" and r["provider"] == "mock"
    assert [e.get("new") for e in events] == ["LISTENING", "THINKING", "SPEAKING", "IDLE"]
    exprs = [a[0] for a, _k in ctx.face.called("set_expression")]
    assert exprs[:3] == ["listening", "thinking", "playful"]
    assert ctx.audio.called("listen")[0][1]["timeout"] == ctx.config.ai.listen_timeout_seconds
    assert ctx.arbiter.snapshot() == {k: None for k in ctx.arbiter.snapshot()}


async def test_nothing_heard(make_brain):
    ctx = make_test_context()
    brain = await make_brain(ctx)
    r = await brain.converse_once()
    assert r["heard"] is None and r["error"] == "nothing_heard"
    assert ctx.audio.spoken == ["I didn't hear that. Can you say it again?"]
    assert ctx.state.state == RobotState.IDLE


async def test_nothing_heard_silent_when_configured(make_brain):
    ctx = make_test_context(ai={"say_didnt_hear": False})
    brain = await make_brain(ctx)
    r = await brain.converse_once()
    assert r["error"] == "nothing_heard" and r["reply"] is None and ctx.audio.spoken == []


async def test_converse_respects_microphone_privacy_and_missing_audio(make_brain):
    ctx = make_test_context()
    ctx.privacy.set(microphone_enabled=False)
    brain = await make_brain(ctx)
    r = await brain.converse_once()
    assert r["error"] == "microphone_disabled" and ctx.audio.called("listen") == []
    ctx.audio = None
    assert (await brain.converse_once())["error"] == "no_audio"


async def test_works_without_face_or_audio(make_brain):
    ctx = make_test_context()
    ctx.audio, ctx.face = None, None
    brain = await make_brain(ctx)
    r = await brain.handle_utterance("hello")
    assert r["reply"] and r["spoken"] is None and ctx.state.state == RobotState.IDLE


async def test_speak_false_only_publishes(make_brain):
    ctx = make_test_context()
    brain = await make_brain(ctx)
    r = await brain.handle_utterance("hello", speak=False)
    assert r["reply"] and ctx.audio.spoken == [] and r["spoken"] is None


@pytest.mark.parametrize(("owner", "priority"), [("activity", Priority.ACTIVITY), ("media", Priority.MEDIA)])
async def test_refuses_politely_when_activity_or_media_holds_audio(make_brain, owner, priority):
    ctx = cloud_context()
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    lease = ctx.arbiter.acquire(owner, ["face", "audio_out", "screen"], priority)
    ctx.state.transition(RobotState.PLAYING_ACTIVITY if owner == "activity" else RobotState.PLAYING_MEDIA, "test")
    before = ctx.state.state
    r = await brain.handle_utterance("hello")
    assert r["error"] == "busy" and r["reply"] is None and "busy" in r["detail"]
    assert ctx.audio.spoken == [] and api.calls == 0 and ctx.state.state == before
    assert ctx.arbiter.holder("audio_out") is lease
    ctx.audio.inject_utterance("hi")
    assert (await brain.converse_once())["error"] == "busy"


async def test_preempts_lower_priority_reactions(make_brain):
    ctx = make_test_context()
    preempted = []
    ctx.arbiter.acquire("reaction", ["face"], Priority.REACTIVE, on_preempt=lambda lease, by: preempted.append(by))
    brain = await make_brain(ctx)
    r = await brain.handle_utterance("hello")
    assert r["error"] is None and preempted == ["conversation"]


async def test_second_turn_while_busy_is_refused(make_brain):
    ctx = cloud_context()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def blocked(req):
        entered.set()
        await gate.wait()
        return anthropic_response()

    brain = await make_brain(ctx, FakeAPI(blocked))
    first = asyncio.create_task(brain.handle_utterance("hello"))
    await asyncio.wait_for(entered.wait(), 2)
    assert brain.status()["busy"] is True
    assert (await brain.handle_utterance("me too"))["error"] == "busy"
    gate.set()
    assert (await first)["error"] is None


async def _start_hanging_turn(brain, entered):
    task = asyncio.create_task(brain.handle_utterance("tell me about cows"))
    await asyncio.wait_for(entered.wait(), 2)
    return task


async def test_stop_all_cancels_request_clears_context_and_frees_leases(make_brain):
    ctx = cloud_context()
    entered = asyncio.Event()
    calls = {"n": 0}

    async def respond(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return anthropic_response("Cows say moo!")
        entered.set()
        await asyncio.sleep(30)
        return anthropic_response()

    brain = await make_brain(ctx, FakeAPI(respond))
    await brain.handle_utterance("cows")
    assert brain.status()["context_turns"] == 1
    task = await _start_hanging_turn(brain, entered)
    t0 = time.monotonic()
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    r = await asyncio.wait_for(task, 2)
    assert time.monotonic() - t0 < 1.0
    assert r["error"] == "stopped" and r["reply"] is None
    assert ctx.audio.spoken == ["Cows say moo!"]  # nothing new was said
    assert brain.status()["context_turns"] == 0 and brain.status()["busy"] is False
    assert ctx.state.state == RobotState.IDLE
    assert all(v is None for v in ctx.arbiter.snapshot().values())


class SlowAudio(FakeAudio):
    def __init__(self, bus):
        super().__init__(bus)
        self.speaking_started = asyncio.Event()

    async def say(self, text, *, rate=None):
        self._rec("say", text, rate=rate)
        self.speaking_started.set()
        await asyncio.sleep(30)
        return True


async def test_stop_all_while_speaking_stops_output(make_brain):
    ctx = make_test_context()
    ctx.audio = SlowAudio(ctx.bus)
    brain = await make_brain(ctx)
    task = asyncio.create_task(brain.handle_utterance("hello"))
    await asyncio.wait_for(ctx.audio.speaking_started.wait(), 2)
    assert ctx.state.state == RobotState.SPEAKING
    ctx.bus.publish(Topics.STOP_ALL, {"source": "parent"})
    r = await asyncio.wait_for(task, 2)
    assert r["error"] == "stopped" and ctx.audio.called("stop_output")
    assert ctx.state.state == RobotState.IDLE


async def test_preempted_by_activity_does_not_clobber_its_state(make_brain):
    ctx = cloud_context()
    entered = asyncio.Event()

    async def hang(req):
        entered.set()
        await asyncio.sleep(30)
        return anthropic_response()

    brain = await make_brain(ctx, FakeAPI(hang))
    task = await _start_hanging_turn(brain, entered)
    lease = ctx.arbiter.acquire("activity", ["face", "audio_out", "microphone"], Priority.ACTIVITY)
    assert lease is not None
    ctx.state.transition(RobotState.PLAYING_ACTIVITY, "activity started")
    r = await asyncio.wait_for(task, 2)
    assert r["error"] == "stopped"
    assert ctx.state.state == RobotState.PLAYING_ACTIVITY and ctx.arbiter.holder("audio_out") is lease
    assert ctx.audio.spoken == []


async def test_parent_preemption_without_state_change_returns_to_idle(make_brain):
    ctx = cloud_context()
    entered = asyncio.Event()

    async def hang(req):
        entered.set()
        await asyncio.sleep(30)
        return anthropic_response()

    brain = await make_brain(ctx, FakeAPI(hang))
    task = await _start_hanging_turn(brain, entered)
    assert ctx.state.state == RobotState.THINKING
    lease = ctx.arbiter.acquire("dashboard", ["audio_out"], Priority.PARENT)  # e.g. parent plays a chime
    r = await asyncio.wait_for(task, 2)
    assert r["error"] == "stopped" and ctx.state.state == RobotState.IDLE
    assert ctx.arbiter.holder("audio_out") is lease and ctx.arbiter.holder("face") is None


async def test_sleep_clears_context_and_utterance_wakes(make_brain):
    ctx = cloud_context()
    brain = await make_brain(ctx, FakeAPI())
    await brain.handle_utterance("hello")
    assert brain.status()["context_turns"] == 1
    ctx.state.transition(RobotState.SLEEPING, "night")
    assert brain.status()["context_turns"] == 0
    r = await brain.handle_utterance("wake up")
    assert r["error"] is None and ctx.state.state == RobotState.IDLE


async def test_context_is_tiny_and_expires(make_brain):
    ctx = cloud_context(ai={"context_turns": 2})
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    for i in range(4):
        await brain.handle_utterance(f"turn {i}")
    assert len(api.body()["messages"]) == 5  # 2 remembered turns + the new one
    assert api.body()["messages"][0]["content"] == "turn 1"
    ctx2 = cloud_context(ai={"context_ttl_seconds": 0.0})
    api2 = FakeAPI()
    brain2 = await make_brain(ctx2, api2)
    await brain2.handle_utterance("one")
    await asyncio.sleep(0.01)
    await brain2.handle_utterance("two")
    assert len(api2.body()["messages"]) == 1


# --------------------------------------------------------- safety & privacy
async def test_concerning_input_alerts_the_parent_and_the_cloud_is_told(make_brain):
    ctx = cloud_context()
    events = collect(ctx.bus, [Topics.BRAIN_STATUS, Topics.BRAIN_REPLY])
    api = FakeAPI(lambda req: anthropic_response("Oh no! Let's find a grown-up to help you right now. [face: happy]"))
    brain = await make_brain(ctx, api)
    r = await brain.handle_utterance("I'm hurt, I fell off the swing at Grandma's")
    assert api.calls == 1 and r["provider"] == "anthropic" and r["attention"] == "hurt"
    assert "may be about an emergency" in api.body()["system"] and "hurt or unwell" in api.body()["system"]
    assert r["reply"] == "Oh no! Let's find a grown-up to help you right now."
    assert ctx.audio.spoken == [r["reply"]] and r["expression"] == "affectionate" and "face_action" not in r
    alert = [e for e in events if e.topic == Topics.BRAIN_STATUS and e.get("status") == "attention"]
    assert len(alert) == 1 and alert[0].data == {"provider": "safety", "status": "attention", "detail": "hurt"}
    assert "swing" not in json.dumps([e.data for e in events])


async def test_a_grownups_emergency_question_gets_real_help(make_brain):
    # Worrying words from a parent ("my baby swallowed a coin") used to get only "tell a grown-up".
    help_text = ("If she's breathing and comfortable, call your doctor or 111 now. If she's choking or "
                 "struggling to breathe, call 999.")
    ctx = cloud_context(age_months=10)
    api = FakeAPI(lambda req: anthropic_response(help_text))
    brain = await make_brain(ctx, api)
    r = await brain.handle_utterance("my baby swallowed a coin, what should I do?")
    assert r["attention"] == "swallowed" and r["reply"] == help_text and r["filtered"] is False
    assert "swallowed something harmful" in api.body()["system"]


@pytest.mark.parametrize("respond", [
    lambda req: anthropic_response("Yay, let's play a game! Climb the bookcase!"),  # blocked: never a game
    lambda req: error_response(500, "api_error", "boom"),  # failed: never "let's play instead"
])
async def test_worrying_words_get_the_gentle_line_when_the_cloud_answer_cant_be_used(make_brain, respond):
    ctx = cloud_context(age_months=30)
    brain = await make_brain(ctx, FakeAPI(respond))
    r = await brain.handle_utterance("I ate a battery")
    assert r["attention"] == "swallowed" and r["reply"] == "Uh oh. Please tell a grown-up right now."
    assert r["expression"] == "affectionate" and "_tools" not in r


REVIEW_EMERGENCIES = [
    ("I ate a battery", "swallowed"), ("I swallowed a coin", "swallowed"), ("I drank the soap", "swallowed"),
    ("daddy hit me", "safety"), ("a man said come with me", "stranger"), ("the baby is in the pool", "water"),
    ("help me", "help"), ("I can't breathe", "emergency"),
]


@pytest.mark.parametrize(("text", "category"), REVIEW_EMERGENCIES)
@pytest.mark.parametrize("provider", ["mock", "anthropic"])
async def test_emergencies_get_a_grownup_reply_and_a_parent_alert(make_brain, text, category, provider):
    # Review finding: a 30-month-old saying "I ate a battery" got "Ooh, tell me more!" and no alert.
    ctx = cloud_context(age_months=30, provider=provider)
    events = collect(ctx.bus, [Topics.BRAIN_STATUS, Topics.BRAIN_REPLY])
    api = FakeAPI(lambda req: anthropic_response("Please tell a grown-up right now."))
    brain = await make_brain(ctx, api)
    r = await brain.handle_utterance(text)
    if provider == "mock":  # no cloud: LittleBot's own gentle line, at once
        assert api.calls == 0 and r["provider"] == "safety"
    else:  # the cloud answers, told it may be an emergency (a grown-up may be asking for help)
        assert api.calls == 1 and r["provider"] == "anthropic"
        assert "may be about an emergency" in api.body()["system"]
    assert r["attention"] == category
    assert "grown-up" in r["reply"] and r["expression"] == "affectionate"
    for cheerful in ("fun", "wow", "ooh", "yay", "show me", "tell me more", "clap"):
        assert cheerful not in r["reply"].lower()
    assert ctx.audio.spoken == [r["reply"]]
    alerts = [e.data for e in events if e.topic == Topics.BRAIN_STATUS and e.get("status") == "attention"]
    assert alerts == [{"provider": "safety", "status": "attention", "detail": category}]
    # Only the category label leaves the brain - never the child's words.
    dumped = json.dumps([e.data for e in events]).lower()
    for word in ("battery", "coin", "soap", "daddy", "man said", "pool", "help me", "breathe"):
        assert word not in dumped


async def test_concern_beyond_the_input_length_cap_is_still_caught(make_brain):
    ctx = cloud_context(age_months=40, ai={"max_input_chars": 40})
    api = FakeAPI(lambda req: anthropic_response("Uh oh. Please tell a grown-up right now."))
    brain = await make_brain(ctx, api)
    r = await brain.handle_utterance("I like the red truck and the blue truck and also I swallowed a coin")
    assert r["attention"] == "swallowed" and "grown-up" in r["reply"]
    assert "swallowed something harmful" in api.body()["system"]  # the cut-off words are named for the model


async def test_good_cloud_safety_advice_is_spoken_and_alerts_the_parent(make_brain):
    # Review finding: this good reply used to be blocked as "unsafe" and replaced by "Can you clap your hands?".
    advice = "Oh no! Swallowing a battery is not safe. Tell a grown-up right now!"
    ctx = cloud_context(age_months=40)
    events = collect(ctx.bus, [Topics.BRAIN_STATUS])
    replies = iter(["It looks shiny and round!", advice])
    api = FakeAPI(lambda req: anthropic_response(next(replies)))
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("what is this shiny round thing")  # not itself flagged: the cloud answers
    assert brain.status()["context_turns"] == 1
    r = await brain.handle_utterance("it was in my tummy now")
    assert api.calls == 2 and r["provider"] == "anthropic"
    assert r["reply"] == advice and r["filtered"] is False and r["attention"] == "danger"
    assert r["expression"] == "affectionate" and ctx.audio.spoken[-1] == advice
    alerts = [e.data for e in events if e.get("status") == "attention"]
    assert alerts == [{"provider": "anthropic", "status": "attention", "detail": "danger"}]
    assert brain.status()["context_turns"] == 0  # a possible emergency is not carried into later turns


@pytest.mark.parametrize("bad", ["Touch the stove and tell a grown-up right now!",
                                 "The stove is hot, touch it, then tell a grown-up right now."])
async def test_hazard_plus_grownup_reply_gets_a_gentle_line_not_a_game(make_brain, bad):
    ctx = cloud_context(age_months=40)
    events = collect(ctx.bus, [Topics.BRAIN_STATUS])
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response(bad)))
    r = await brain.handle_utterance("what is that")
    assert r["filtered"] is True and r["reply"] == GROWNUP_FALLBACK and r["attention"] == "danger"
    assert r["reply"] not in SAFE_FALLBACKS + SHORT_SAFE_FALLBACKS and r["expression"] == "affectionate"
    assert [e.data["detail"] for e in events if e.get("status") == "attention"] == ["danger"]


async def test_personal_info_is_scrubbed_before_cloud(make_brain):
    ctx = cloud_context(nickname="Emma")
    ctx.settings.set("profile", {"age_months": 40, "nickname": "Emma", "favourite_animals": ["otters"],
                                 "interests": ["trains"]})
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("Emma lives at 12 Maple Street, call 555-123-4567 or mum@example.com")
    raw = api.requests[0].content.decode()
    for leaked in ("Emma", "Maple", "555", "example.com", "otters", "trains"):
        assert leaked not in raw, leaked
    assert "[name]" in raw and "[phone]" in raw


async def test_parent_opt_in_shares_name_and_interests(make_brain):
    ctx = cloud_context(nickname="Emma", ai={"share_child_name": True, "share_interests": True})
    ctx.settings.set("profile", {"age_months": 40, "nickname": "Emma", "favourite_animals": ["otters"],
                                 "interests": ["trains"]})
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("hi")
    system = api.body()["system"]
    assert "nickname is Emma" in system and "otters" in system and "trains" in system


async def test_request_contains_only_text(make_brain):
    ctx = cloud_context()
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("look at me")
    body = api.body()
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config"}
    assert all(isinstance(m["content"], str) for m in body["messages"])
    assert "image" not in api.requests[0].content.decode()


async def test_no_transcripts_persisted_by_default(make_brain):
    ctx = cloud_context(nickname="")
    brain = await make_brain(ctx, FakeAPI(lambda req: anthropic_response("Purple dinosaurs dance!")))
    for text in ("my purple pancake wish", "hello zebra"):
        await brain.handle_utterance(text)
    ctx.audio.inject_utterance("banana sandwich")
    await brain.converse_once()
    tables = {r["name"] for r in ctx.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"kv"}
    items = ctx.db.kv_items("")
    assert all(k.startswith(("setting:", "brain.usage.")) for k in items)
    dump = json.dumps(items)
    for word in ("pancake", "zebra", "banana", "Purple", "dinosaurs"):
        assert word not in dump
    assert brain.status()["store_transcripts"] is False


async def test_transcripts_need_config_and_parent_opt_in(make_brain):
    ctx = cloud_context(ai={"store_transcripts": True})
    brain = await make_brain(ctx, FakeAPI())
    await brain.handle_utterance("hello there")
    assert ctx.db.query("SELECT COUNT(*) AS n FROM brain_transcript")[0]["n"] == 0  # record_history off
    ctx.privacy.set(record_history=True)
    await brain.handle_utterance("call 555-123-4567")
    rows = ctx.db.query("SELECT role, text FROM brain_transcript ORDER BY id")
    assert [r["role"] for r in rows] == ["child", "robot"] and "555" not in rows[0]["text"]
    brain.clear_history()
    assert ctx.db.query("SELECT COUNT(*) AS n FROM brain_transcript")[0]["n"] == 0


async def test_keys_never_in_logs_status_or_events(make_brain, caplog):
    caplog.set_level(logging.DEBUG)
    ctx = cloud_context()
    events = collect(ctx.bus, ["*"])
    api = FakeAPI(lambda req: error_response(401, "authentication_error", f"bad key {ANTHROPIC_KEY}"))
    brain = await make_brain(ctx, api)
    await brain.handle_utterance("secret word: giraffe pancakes")
    await brain.test_provider("anthropic")
    api.respond = _route
    ctx.settings.set("ai.max_words", 20)
    await brain.handle_utterance("giraffe pancakes again")
    ctx.settings.set("ai.provider", "openai")
    await brain.handle_utterance("giraffe pancakes thrice")
    await brain.test_provider("openai")
    blobs = [caplog.text, json.dumps(brain.status()), json.dumps(brain.providers()),
             json.dumps([e.data for e in events], default=str), repr(brain.__dict__)]
    for blob in blobs:
        assert ANTHROPIC_KEY not in blob and OPENAI_KEY not in blob and "TESTKEY" not in blob
    assert "giraffe" not in caplog.text  # no transcripts in logs either


# ------------------------------------------------------------ test_provider
async def test_test_provider(make_brain):
    ctx = cloud_context()
    api = FakeAPI()
    brain = await make_brain(ctx, api)
    mock = await brain.test_provider("mock")
    assert mock["ok"] and mock["error"] is None and api.calls == 0
    ok = await brain.test_provider("anthropic")
    assert ok["ok"] and ok["latency_ms"] >= 0 and ok["model"] == "claude-haiku-5-5" and api.calls == 1
    assert api.body()["max_tokens"] <= 16 + ctx.config.ai.thinking_headroom_tokens
    assert (await brain.test_provider("nope"))["ok"] is False
    api.respond = lambda req: error_response(401, "authentication_error", "invalid x-api-key")
    bad = await brain.test_provider("anthropic")
    assert bad == {**bad, "ok": False, "error": "auth_error"}
    assert brain.status()["provider_status"]["anthropic"] == "auth_error"
    api.respond = lambda req: anthropic_response()
    assert (await brain.test_provider("anthropic"))["ok"]
    assert brain.status()["provider_status"]["anthropic"] == "ok"


async def test_test_provider_needs_cloud_permission_and_key(make_brain):
    api = FakeAPI()
    brain = await make_brain(cloud_context(cloud=False), api)
    assert (await brain.test_provider("anthropic"))["error"] == "cloud_disabled"
    brain2 = await make_brain(cloud_context(key=False), api)
    assert (await brain2.test_provider("openai"))["error"] == "no_api_key"
    brain3 = await make_brain(cloud_context(mode="offline"), api)
    assert (await brain3.test_provider("anthropic"))["error"] == "cloud_disabled"
    assert api.calls == 0


# ----------------------------------------------------- utterance events
async def test_replies_to_utterance_events_only_when_idle(make_brain):
    ctx = make_test_context()
    await make_brain(ctx)
    ctx.bus.publish(Topics.AUDIO_UTTERANCE, {"text": "hello", "confidence": 1.0, "source": "simulated"})
    ev = await ctx.bus.wait_for(Topics.BRAIN_REPLY, timeout=2)
    assert ev is not None and ev.get("text") == "Hi hi! Wave wave!"
    await asyncio.sleep(0.05)
    ctx.state.transition(RobotState.PLAYING_ACTIVITY, "activity")
    ctx.bus.publish(Topics.AUDIO_UTTERANCE, {"text": "moo", "confidence": 1.0, "source": "stt"})
    assert await ctx.bus.wait_for(Topics.BRAIN_REPLY, timeout=0.2) is None
    assert ctx.audio.spoken == ["Hi hi! Wave wave!"]


async def test_utterance_event_echo_of_handled_turn_is_ignored(make_brain):
    ctx = make_test_context()
    ctx.audio, ctx.face = None, None  # turn completes without yielding to the loop
    replies = collect(ctx.bus, [Topics.BRAIN_REPLY])
    brain = await make_brain(ctx)
    await brain.handle_utterance("hello")
    ctx.bus.publish(Topics.AUDIO_UTTERANCE, {"text": "Hello", "source": "stt"})
    await asyncio.sleep(0.05)
    assert len(replies) == 1


async def test_utterance_events_can_be_disabled(make_brain):
    ctx = make_test_context(ai={"reply_to_utterance_events": False})
    await make_brain(ctx)
    ctx.bus.publish(Topics.AUDIO_UTTERANCE, {"text": "hello", "source": "simulated"})
    assert await ctx.bus.wait_for(Topics.BRAIN_REPLY, timeout=0.2) is None


# ------------------------------------------------- robot integration (threads)
class HangingCloud(BaseProvider):
    name, is_cloud, model = "anthropic", True, "hanging"

    def __init__(self):
        self.started = None

    async def reply(self, messages, *, system, max_tokens, timeout):
        self.started.set()
        await asyncio.sleep(30)
        return ProviderReply("never")


def test_robot_stop_all_from_another_thread_cancels_turn():
    import threading

    from littlebot.app.robot import LittleBot

    ctx = cloud_context(dashboard={"enabled": False})
    provider = HangingCloud()
    provider.started = threading.Event()
    ctx.brain = ConversationEngine(ctx, providers={"anthropic": provider})
    bot = LittleBot(ctx)
    bot.build_subsystems(only={"brain"})
    bot.start()
    try:
        fut = bot.runtime.submit(ctx.brain.handle_utterance("hello"))
        assert provider.started.wait(3)
        bot.runtime.run(bot.stop_all("test"), timeout=5)
        result = fut.result(timeout=3)
        assert result["error"] == "stopped"
        assert ctx.state.state == RobotState.IDLE and not ctx.brain.status()["busy"]
        assert bot.status()["brain"]["active_provider"] == "anthropic"
    finally:
        bot.shutdown("test")
