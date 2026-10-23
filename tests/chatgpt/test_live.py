"""Live voice against a local WebRTC peer standing in for OpenAI (real aiortc call over this machine)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402

import littlebot.chatgpt.live as live_mod  # noqa: E402
from littlebot.brain.providers.base import ProviderReply  # noqa: E402
from littlebot.chatgpt.live import NOT_SURE, LiveVoiceService  # noqa: E402
from littlebot.core.arbiter import Priority, Resource  # noqa: E402
from littlebot.core.events import Topics  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class LendingAudio:
    """Records the audio service hand-over."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        self.calls.append(f"suspend:{owner}")

    def resume(self, owner: str) -> None:
        self.calls.append(f"resume:{owner}")

    def status(self) -> dict[str, Any]:
        return {"volume": 0.5}


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
async def live(ctx, tmp_path):
    ctx.privacy.set(cloud_enabled=True)
    ctx.audio = LendingAudio()
    server = FakeOpenAI()
    audio = FakeLiveAudio()
    provider = FakeProvider()
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                           audio_factory=lambda: audio, provider=provider)
    await svc.start()
    alerts: list[dict[str, Any]] = []
    ctx.bus.subscribe(Topics.BRAIN_STATUS, lambda e: alerts.append(dict(e.data)))
    yield svc, server, audio, provider, alerts
    await svc.stop()
    await server.close()


async def connect(svc: LiveVoiceService, server: FakeOpenAI) -> None:
    result = await svc.start_session(parent_present=True)
    assert result["ok"], result
    await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")


async def test_call_connects_plays_voice_and_cleans_up(live, ctx):
    svc, server, audio, _provider, _alerts = live
    await connect(svc, server)
    req = server.requests[0]
    assert req["url"].endswith("/codex/realtime/calls?intent=quicksilver&architecture=avas")
    assert req["headers"]["openai-alpha"] == "quicksilver=v2"
    assert req["headers"]["chatgpt-account-id"] == "acct-123"
    assert req["headers"]["authorization"].startswith("Bearer ")
    session = req["body"]["session"]
    assert session["model"] == "gpt-live-1-codex" and session["delegation"] == {"type": "client"}
    assert "Let's get a grown-up right now" in session["instructions"]
    assert req["body"]["sdp"].startswith("v=0")
    assert ctx.audio.calls == ["suspend:live voice"]
    assert ctx.arbiter.holder(Resource.MICROPHONE).owner == "live_voice"
    assert ctx.state.state == RobotState.LISTENING
    await wait_for(lambda: audio.frames > 20)  # the peer's tone reached the speaker
    assert audio.peak > 1000
    await svc.stop_session()
    st = svc.status()
    assert not st["active"] and st["last_call"]["ended_reason"] == "stopped by parent"
    assert ctx.audio.calls == ["suspend:live voice", "resume:live voice"]
    assert ctx.arbiter.holder(Resource.MICROPHONE) is None
    assert ctx.state.state == RobotState.IDLE
    assert audio.closed


async def test_delegation_is_answered_by_the_text_model(live):
    svc, server, _audio, provider, _alerts = live
    await connect(svc, server)
    server.send({"type": "input_transcript.added", "item": {"text": "what does a cow say"}})
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "what does a cow say"}})
    server.send({"type": "delegation.created", "item": {"id": "d1", "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": "What sound does a cow make?"}]}})
    await wait_for(lambda: any(m.get("type") == "delegation.context.append" for m in server.received))
    frame = next(m for m in server.received if m.get("type") == "delegation.context.append")
    assert frame["delegation_item_id"] == "d1" and frame["channel"] == "speakable"
    assert frame["content"][0]["text"] == "A cow says moo."
    assert provider.calls[0]["messages"][-1] == {"role": "user", "content": "What sound does a cow make?"}
    assert "friendly robot" in provider.calls[0]["system"] and "home assistant" in provider.calls[0]["system"]
    assert {"who": "heard", "text": "what does a cow say"} in svc.status()["captions"]


async def test_unsafe_delegation_answer_is_replaced(live):
    svc, server, _audio, provider, _alerts = live
    provider.text = "Let's keep this a secret from your mommy."
    await connect(svc, server)
    server.send({"type": "delegation.created", "item": {"id": "d2", "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": "a question"}]}})
    await wait_for(lambda: any(m.get("type") == "delegation.context.append" for m in server.received))
    text = next(m for m in server.received if m.get("type") == "delegation.context.append")["content"][0]["text"]
    assert "secret" not in text and text == NOT_SURE


async def test_unsafe_words_cut_the_voice_and_end_the_call(live, ctx):
    svc, server, audio, _provider, alerts = live
    await connect(svc, server)
    for part in ("Hello little one! ", "This is our little ", "secret, don't tell mommy."):
        server.send({"type": "output_transcript.added", "item": {"text": part}})
    await wait_for(lambda: not svc.status()["active"])
    last = svc.status()["last_call"]
    assert last["safety_stop"] == "secrecy"
    assert audio.muted
    assert {"provider": "live", "status": "live_safety_stop", "detail": "secrecy"} in alerts
    assert "captions" not in svc.status()  # nothing from the conversation is kept
    assert ctx.arbiter.holder(Resource.AUDIO_OUT) is None


async def test_a_personal_question_is_noted_and_the_call_goes_on(live):
    svc, server, audio, _provider, alerts = live
    await connect(svc, server)
    for part in ("Hello little one! ", "What's your name? ", "Can you clap?"):
        server.send({"type": "output_transcript.added", "item": {"text": part}})
    await wait_for(lambda: any("personal details" in n for n in notes(server)))
    await asyncio.sleep(0.2)
    assert svc.status()["active"] and not audio.muted and svc.status().get("safety_stop") is None
    assert sum("personal details" in n for n in notes(server)) == 1  # once, though checked again as words arrive
    assert not any(a.get("status") == "live_safety_stop" for a in alerts)


async def test_everyday_answers_do_not_stop_the_call(live):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    said = ("It's 18 degrees and sunny in Leeds. The Met Office website says showers after 4pm. "
            "Shall I find a rainy-day song on YouTube? Remember, never leave a baby alone in the bath.")
    for part in said.split(". "):
        server.send({"type": "output_transcript.added", "item": {"text": part + ". "}})
    server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": said}})
    await wait_for(lambda: any(c["who"] == "robot" for c in svc.status().get("captions", [])))
    assert svc.status()["active"]


async def test_a_weather_hand_off_answer_is_passed_on_as_it_is(live):
    svc, server, _audio, provider, _alerts = live
    provider.text = "It's 18 degrees and sunny in Leeds today; the Met Office website says showers after 4pm."
    await connect(svc, server)
    delegate(server, "w1", "What's the weather like today?")
    await wait_for(lambda: answers(server, "w1"))
    assert answers(server, "w1") == [provider.text]
    assert "never make up live facts" in provider.calls[0]["system"]


async def test_a_childs_medicine_dose_in_a_hand_off_becomes_check_the_label(live):
    from littlebot.safety.content import DOSE_FALLBACK

    svc, server, _audio, provider, _alerts = live
    provider.text = "For a 10 month old, give 2.5 ml of infant paracetamol."
    await connect(svc, server)
    delegate(server, "m1", "How much paracetamol can I give my baby?")
    await wait_for(lambda: answers(server, "m1"))
    assert answers(server, "m1") == [DOSE_FALLBACK]


async def test_safe_words_do_not_stop_the_call(live):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    for part in ("Moo! ", "The cow says moo. ", "Can you clap your hands?"):
        server.send({"type": "output_transcript.added", "item": {"text": part}})
    server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": "Moo! The cow says moo. Can you clap your hands?"}})
    await wait_for(lambda: any(c["who"] == "robot" for c in svc.status().get("captions", [])))
    assert svc.status()["active"]


async def test_worrying_words_alert_the_parent(live):
    svc, server, _audio, _provider, alerts = live
    await connect(svc, server)
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "help I'm hurt"}})
    await wait_for(lambda: any(a.get("status") == "attention" for a in alerts))
    assert svc.status()["active"]


async def test_stop_all_ends_the_call(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    ctx.bus.publish(Topics.STOP_ALL, {"source": "test"})
    await wait_for(lambda: not svc.status()["active"])
    assert svc.status()["last_call"]["ended_reason"] == "stopped (STOP ALL)"


async def test_turning_cloud_off_ends_the_call(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    ctx.privacy.set(cloud_enabled=False)
    await wait_for(lambda: not svc.status()["active"])


async def test_time_limit_says_goodbye_then_hangs_up(live, ctx):
    svc, server, _audio, _provider, _alerts = live
    svc.cfg.live_session_minutes = 0.02  # 1.2 s
    await connect(svc, server)
    await wait_for(lambda: any(m.get("type") == "session.context.append" for m in server.received))
    await wait_for(lambda: not svc.status()["active"], timeout=20)
    assert svc.status()["last_call"]["ended_reason"] == "time limit reached"


async def test_idle_hang_up(live):
    svc, server, _audio, _provider, _alerts = live
    svc.cfg.live_idle_hangup_seconds = 0.5
    import littlebot.chatgpt.live as live_mod

    old = live_mod.FIRST_WORDS_MIN_S
    live_mod.FIRST_WORDS_MIN_S = 0.5
    try:
        await connect(svc, server)
        await wait_for(lambda: not svc.status()["active"])
    finally:
        live_mod.FIRST_WORDS_MIN_S = old
    assert "quiet" in svc.status()["last_call"]["ended_reason"]


async def test_server_closing_the_call_is_reported(live):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    server.send({"type": "session.closed", "reason": "server_error"})
    await wait_for(lambda: not svc.status()["active"])
    assert svc.status()["last_call"]["error"] == "ChatGPT ended the call."


@pytest.mark.parametrize("status,needle", [(403, "not available"), (429, "usage limit"), (404, "unavailable or changed")])
async def test_call_errors_are_friendly(ctx, tmp_path, status, needle):
    ctx.privacy.set(cloud_enabled=True)
    ctx.audio = LendingAudio()
    server = FakeOpenAI(status=status)
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                           audio_factory=FakeLiveAudio, provider=FakeProvider())
    await svc.start()
    try:
        assert (await svc.start_session(parent_present=True))["ok"]
        await wait_for(lambda: not svc.status()["active"])
        assert needle in svc.status()["last_call"]["error"]
        assert ctx.audio.calls == ["suspend:live voice", "resume:live voice"]
        assert ctx.arbiter.holder(Resource.MICROPHONE) is None
        assert ctx.state.state == RobotState.IDLE
    finally:
        await svc.stop()


async def test_gates(ctx, tmp_path):
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(FakeOpenAI()),
                           audio_factory=FakeLiveAudio, provider=FakeProvider())
    r = await svc.start_session(parent_present=True)
    assert r == {"ok": False, "error": "cloud_off", "detail": r["detail"]}
    ctx.privacy.set(cloud_enabled=True)
    assert (await svc.start_session(parent_present=False))["error"] == "parent_not_present"
    ctx.privacy.set(microphone_enabled=False)
    assert (await svc.start_session(parent_present=True))["error"] == "mic_off"
    ctx.privacy.set(microphone_enabled=True)
    await svc.auth.sign_out()
    assert (await svc.start_session(parent_present=True))["error"] == "not_signed_in"


async def test_busy_when_something_more_important_holds_the_speaker(ctx, tmp_path):
    ctx.privacy.set(cloud_enabled=True)
    ctx.arbiter.acquire("system", [Resource.AUDIO_OUT], Priority.SYSTEM)
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(FakeOpenAI()),
                           audio_factory=FakeLiveAudio, provider=FakeProvider())
    assert (await svc.start_session(parent_present=True))["error"] == "busy"


async def test_unsafe_words_late_in_a_long_answer_are_caught(live):
    svc, server, audio, _provider, _alerts = live
    await connect(svc, server)
    for _ in range(12):
        server.send({"type": "output_transcript.added", "item": {"text": "The cow says moo and the duck says quack. "}})
    for part in ("Now come closer. ", "This is just our ", "secret, okay?"):
        server.send({"type": "output_transcript.added", "item": {"text": part}})
    await wait_for(lambda: not svc.status()["active"])
    assert svc.status()["last_call"]["safety_stop"] == "secrecy" and audio.muted


async def test_default_voice_is_bubbie(live):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    assert server.requests[0]["body"]["session"]["audio"]["output"]["voice"] == "bubbie"
    assert svc.status()["voice_in_use"] == "bubbie"


async def test_unavailable_voice_falls_back_to_sol(ctx, tmp_path):
    ctx.privacy.set(cloud_enabled=True)
    ctx.audio = LendingAudio()
    server = FakeOpenAI(reject_voices=("bubbie",))
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                           audio_factory=FakeLiveAudio, provider=FakeProvider())
    await svc.start()
    try:
        await connect(svc, server)
        assert [r["body"]["session"]["audio"]["output"]["voice"] for r in server.requests] == ["bubbie", "sol"]
        assert svc.status()["voice_in_use"] == "sol"
    finally:
        await svc.stop()
        await server.close()


class FakeLook:
    """Stands in for CameraLook: ``asked`` counts the looks (each one would be a photo)."""

    def __init__(self, blocked: str | None = None, answer: str = "You are holding up three fingers.",
                 error: str | None = None, detail: str | None = None, delay: float = 0.0) -> None:
        self.blocked, self.answer, self.error, self.detail, self.delay = blocked, answer, error, detail, delay
        self.asked: list[tuple[str, str]] = []

    def blocked_reason(self) -> str | None:
        return self.blocked

    async def look(self, question: str, *, asked_by: str = "parent") -> dict[str, Any]:
        self.asked.append((question, asked_by))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            return {"ok": False, "answer": None, "error": self.error, "detail": self.detail, "latency_ms": 5}
        return {"ok": True, "answer": self.answer, "error": None, "detail": None, "latency_ms": 5}


def delegate(server: FakeOpenAI, did: str, text: str) -> None:
    server.send({"type": "delegation.created", "item": {"id": did, "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": text}]}})


def answers(server: FakeOpenAI, did: str) -> list[str]:
    return [m["content"][0]["text"] for m in server.received
            if m.get("type") == "delegation.context.append" and m.get("delegation_item_id") == did]


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


def hear(server: FakeOpenAI, text: str) -> None:
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": text}})


def note_captions(svc: LiveVoiceService) -> list[str]:
    return [c["text"] for c in svc.status().get("captions", []) if c["who"] == "note"]


async def test_seeing_questions_take_one_photo(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    delegate(server, "v1", "Look at the camera: how many fingers is the person holding up?")
    await wait_for(lambda: answers(server, "v1"))
    assert answers(server, "v1") == ["You are holding up three fingers."]
    assert look.asked == [("Look at the camera: how many fingers is the person holding up?", "live")]
    assert provider.calls == []  # answered from the photo, not the text model
    st = svc.status()
    assert st["photos"] == 1 and any(c["who"] == "note" for c in st["captions"])
    delegate(server, "v2", "What is the capital of France?")  # not about seeing: no photo
    await wait_for(lambda: answers(server, "v2"))
    assert len(look.asked) == 1 and len(provider.calls) == 1


async def test_look_up_is_not_a_look(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    delegate(server, "u1", "Look up what sound a cow makes")
    await wait_for(lambda: answers(server, "u1"))
    assert answers(server, "u1") == ["A cow says moo."] and look.asked == [] and len(provider.calls) == 1


@pytest.mark.parametrize("reason,needle,note", [
    ("ai_camera_off", "switching on Let the AI look through the camera, on the Privacy page", "Let the AI look"),
    ("camera_off", "my camera is switched off on the Privacy page", "camera is turned off"),
    ("no_camera", "my camera isn't working", "No camera is working"),
    ("not_signed_in", "can't see right now", "No photo taken"),
])
async def test_no_photo_when_the_parent_has_not_allowed_it(live, ctx, reason, needle, note):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(blocked=reason)
    await connect(svc, server)
    delegate(server, "v3", "Look: how many fingers am I holding?")
    await wait_for(lambda: answers(server, "v3"))
    assert look.asked == [] and svc.status()["photos"] == 0
    (text,) = answers(server, "v3")
    assert needle in text and "don't guess" in text  # says why, instead of the text model saying "I can't see"
    assert provider.calls == []
    assert any(c.startswith("No photo taken") and note in c for c in note_captions(svc))
    # a knowledge question that only mentions a colour still goes to the text model
    delegate(server, "v4", "What colour is a banana?")
    await wait_for(lambda: answers(server, "v4"))
    assert answers(server, "v4") == ["A cow says moo."] and look.asked == []


def test_what_the_voice_is_told_when_it_cannot_see_is_safe_to_say():
    from littlebot.chatgpt.live import CANT_LOOK
    from littlebot.safety.content import validate_reply

    for text in CANT_LOOK.values():
        said = text.split('"')[1]  # the words the voice is given to say
        assert not validate_reply(said, max_words=10_000).blocked, said


@pytest.mark.parametrize("code,needle", [("photo_failed", "camera couldn't take a picture"),
                                         ("timeout", "took too long"),
                                         ("network", "couldn't reach ChatGPT to look"),
                                         ("usage_limit", "couldn't reach ChatGPT to look")])
async def test_failed_look_tells_the_voice_why(live, ctx, code, needle):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(error=code, detail=f"Dashboard detail for {code}.")
    await connect(svc, server)
    delegate(server, "e1", "Look: how many fingers am I holding up?")
    await wait_for(lambda: answers(server, "e1"))
    (text,) = answers(server, "e1")
    assert needle in text and "don't guess" in text
    assert len(look.asked) == 1 and svc.status()["photos"] == 0 and provider.calls == []
    assert f"Looking didn't work: Dashboard detail for {code}." in note_captions(svc)


async def test_asking_to_see_looks_even_without_a_hand_off(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    hear(server, "See you later!")  # not about seeing: no photo
    hear(server, "How many fingers am I holding up?")
    await wait_for(lambda: notes(server))
    assert look.asked == [("How many fingers am I holding up?", "live")]
    assert notes(server) == ["You just looked through your camera. You are holding up three fingers. "
                             "Tell them right away, starting with the answer."]
    assert svc.status()["photos"] == 1 and provider.calls == []


async def test_a_hand_off_during_that_look_shares_the_one_photo(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(delay=0.5)
    await connect(svc, server)
    hear(server, "how many fingers am I holding up")
    delegate(server, "s1", "Look: how many fingers is the person holding up?")
    await wait_for(lambda: answers(server, "s1"))
    assert answers(server, "s1") == ["You are holding up three fingers."]
    await asyncio.sleep(1.2)
    assert len(look.asked) == 1 and svc.status()["photos"] == 1
    assert notes(server) == []  # the hand-off got the answer: the voice hears it once


async def test_a_hand_off_just_after_that_look_shares_it_but_a_new_question_looks_again(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    hear(server, "What am I holding?")
    await wait_for(lambda: notes(server))
    delegate(server, "s2", "Look: what is the person holding?")
    await wait_for(lambda: answers(server, "s2"))
    # the note came first: the hand-off still gets the answer, and is told the voice already had it
    assert answers(server, "s2") == [f"You are holding up three fingers. {live_mod.ALREADY_TOLD}"]
    assert len(look.asked) == 1
    hear(server, "and now?")  # something new was said: the next hand-off is a new question
    delegate(server, "s3", "Look again: how many fingers now?")
    await wait_for(lambda: answers(server, "s3"))
    assert len(look.asked) == 2 and svc.status()["photos"] == 2


async def test_a_hand_off_before_the_words_arrive_takes_one_photo(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(delay=0.3)
    await connect(svc, server)
    delegate(server, "s4", "Look: what is the person holding?")
    hear(server, "what am I holding")
    await wait_for(lambda: answers(server, "s4"))
    await asyncio.sleep(1.2)
    assert len(look.asked) == 1 and notes(server) == []


async def test_asking_to_see_when_not_allowed_tells_the_voice_why_once(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(blocked="ai_camera_off")
    await connect(svc, server)
    hear(server, "can you see me?")
    await wait_for(lambda: notes(server))
    (text,) = notes(server)
    assert text.startswith("They asked you to look.") and "Let the AI look through the camera" in text
    assert look.asked == [] and svc.status()["photos"] == 0
    assert any(c.startswith("No photo taken") for c in note_captions(svc))


def test_seeing_words():
    from littlebot.chatgpt.live import asks_to_see

    for text in ("How many fingers am I holding up?", "how many fingers do you see", "What am I holding?",
                 "Guess what I\u2019m holding!", "whats in my hand", "Can you see me?", "Do you see my teddy?",
                 "What colour is this?", "what color are my eyes", "What do you see?", "Look at this!",
                 "count my fingers", "What is she wearing?",
                 # more ways of asking
                 "What is this?", "Hey LittleBot, what's this?", "What is this, LittleBot?", "what's this called",
                 "What is that thing?", "What colour shirt am I wearing?", "what colour is my shirt",
                 "Do you know what this is?", "tell me what these are", "Can you see what I'm doing?",
                 "Look what I made!", "How many blocks can you see?", "What animal is this?", "Which colour is this?",
                 "What does this say?", "Can you read this?", "What have I got here?", "What kind of dog is this?",
                 "what is this I'm holding", "What is the child holding?", "what's in the child's hand"):
        assert asks_to_see(text), text
    for text in ("See you later!", "nice to see you", "How many fingers does a cat have?",
                 "how many fingers do you have", "Do you see what I mean?", "What colour is the sky?",
                 "I like to look up at the clouds", "Make a happy face", "Let me see", "",
                 "What is this song?", "What's that?", "what is that noise", "What are we doing today?",
                 "Can you guess what it is?", "what have I got to do today", "Look what time it is!",
                 "How many legs does a spider have?", "What number is your favourite?", "What is it?",
                 "And now?", "What about this one?"):  # those two only right after a look
        assert not asks_to_see(text), text
    for text in ("And now?", "what about this one", "How many now?", "and this one?"):
        assert asks_to_see(text, follow_up=True), text
    assert not asks_to_see("and now let's sing a song", follow_up=True)


def test_seeing_questions_are_not_face_requests():
    face_actions = pytest.importorskip("littlebot.brain.face_actions")
    for text in ("how many fingers am I holding up?", "Look: how many fingers am I holding?",
                 "Look at the camera: how many fingers is the person holding up?", "What am I holding?",
                 "Can you see me?", "What colour is this?", "Look at this!", "What do you see?"):
        assert face_actions.parse_request(text) is None, text


async def test_face_requests_play_on_the_face(live, ctx):
    pytest.importorskip("littlebot.brain.face_actions")
    svc, server, _audio, provider, _alerts = live
    await connect(svc, server)
    assert "happy" in server.requests[0]["body"]["session"]["instructions"]  # the voice knows its faces
    delegate(server, "f1", "face: surprised")
    await wait_for(lambda: answers(server, "f1"))
    assert answers(server, "f1")[0].startswith("Done")
    await wait_for(lambda: ctx.face.expression == "surprised")
    delegate(server, "f2", "Make a happy face please")
    await wait_for(lambda: ctx.face.expression == "happy")
    assert provider.calls == []  # handled locally, no text model involved


def say(server: FakeOpenAI, *parts: str) -> None:
    """The voice speaks: caption pieces while it talks, then the whole turn."""
    for part in parts:
        server.send({"type": "output_transcript.added", "item": {"text": part}})
    server.send({"type": "turn.done", "turn": {"role": "assistant", "transcript": "".join(parts)}})


async def test_asking_for_a_face_plays_it_without_a_hand_off(live, ctx, caplog):
    pytest.importorskip("littlebot.brain.face_actions")
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    svc, server, _audio, provider, _alerts = live
    await connect(svc, server)
    hear(server, "Make a happy face!")
    await wait_for(lambda: ctx.face.expression == "happy")
    await wait_for(lambda: notes(server))
    assert notes(server) == ["LittleBot is showing happy on its face now."]
    delegate(server, "h1", "face: happy")  # the voice hands it over too: answered, not played twice
    await wait_for(lambda: answers(server, "h1"))
    assert answers(server, "h1")[0].startswith("Done")
    await asyncio.sleep(0.2)
    assert [a for (a, _k) in ctx.face.called("set_expression")] == [("happy",)]
    assert provider.calls == []
    log_text = caplog.text
    assert "face happy played (from=user)" in log_text and "hand-off received (kind=face)" in log_text
    assert "face happy already playing (from=delegation)" in log_text
    assert "first 'delegation.created' event" in log_text
    assert "Make a happy face" not in log_text  # nothing the person said is logged


async def test_the_voice_saying_peekaboo_plays_it_once(live, ctx, caplog):
    pytest.importorskip("littlebot.brain.face_actions")
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    say(server, "Ready? ", "Peek-a-boo! ", "I see you!")
    await wait_for(lambda: ctx.face.called("close_eyes"))
    await wait_for(lambda: ctx.face.called("open_eyes"))
    assert not ctx.face.eyes_closed
    names = [n for (n, _a, _k) in ctx.face.calls if n in ("close_eyes", "open_eyes")]
    assert names == ["close_eyes", "open_eyes"]  # once for the turn, not on every caption piece
    assert "face peekaboo played (from=voice)" in caplog.text


async def test_the_voice_saying_wink_plays_it(live, ctx):
    pytest.importorskip("littlebot.brain.face_actions")
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    say(server, "Twinkle twinkle little star. ")  # not a wink
    say(server, "Watch me! ", "I'm winking at you.")
    await wait_for(lambda: ctx.face.called("set_expression"))
    assert [a for (a, _k) in ctx.face.called("set_expression")] == [("playful",)]  # the playful face winks


def test_faces_the_voice_announces():
    pytest.importorskip("littlebot.brain.face_actions")
    from littlebot.chatgpt.live import _voice_face

    cases = {"Peekaboo!": "peekaboo", "peek a boo, I see you": "peekaboo", "Wink wink!": "wink",
             "Here's my happy face!": "happy", "This is my silly face.": "playful", "Watch me spin my eyes!": "spin_eyes",
             "Twinkle twinkle little star": None, "Show me your happy face!": None, "I'm so happy!": None,
             "Let's look at the book.": None}
    for text, name in cases.items():
        action = _voice_face(text)
        assert (action.name if action else None) == name, text


async def test_look_outcome_is_logged_without_content(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    svc, server, _audio, _provider, _alerts = live
    ctx.extras["camera_look"] = FakeLook(blocked="ai_camera_off")
    await connect(svc, server)
    delegate(server, "l1", "Look: how many fingers am I holding up?")
    await wait_for(lambda: answers(server, "l1"))
    assert "hand-off received (kind=look)" in caplog.text
    assert "look triggered (from=delegation) -> blocked:ai_camera_off" in caplog.text
    assert "fingers" not in caplog.text


async def test_everyday_words_do_not_change_the_face(live, ctx):
    pytest.importorskip("littlebot.brain.face_actions")
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    before = ctx.face.expression
    for text in ("that makes me sad", "give me a hug", "bounce the ball"):
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": text}})
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "make a surprised face"}})
    await wait_for(lambda: ctx.face.expression == "surprised")
    assert before != "sad" and ctx.face.called("set_expression")[0][0][0] == "surprised"


async def test_a_crashing_or_hanging_look_up_still_answers_the_voice(live, monkeypatch):
    import littlebot.chatgpt.live as live_mod
    from littlebot.chatgpt import wire

    svc, server, _audio, provider, _alerts = live
    await connect(svc, server)

    async def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(provider, "reply", boom)
    delegate(server, "h1", "What is the capital of France?")
    await wait_for(lambda: answers(server, "h1"))
    assert answers(server, "h1") == [wire.FAILED_RESULT]

    async def hang(*_a, **_k):
        await asyncio.sleep(60)

    monkeypatch.setattr(provider, "reply", hang)
    monkeypatch.setattr(live_mod, "HANDOFF_TIMEOUT_S", 0.3)
    delegate(server, "h2", "How tall is a giraffe?")
    await wait_for(lambda: answers(server, "h2"), timeout=5)
    assert answers(server, "h2") == [wire.FAILED_RESULT]
    assert svc.status()["active"]


# --------------------------------------------------------------------------- faster looks
def partial(server: FakeOpenAI, text: str) -> None:
    server.send({"type": "input_transcript.added", "item": {"text": text}})


class StreamingLook(FakeLook):
    """A look whose answer's first sentence arrives before the rest (as CameraLook streams it)."""

    def __init__(self, first: str = "A red ball!", rest: str | None = "It is on the green rug.",
                 delay: float = 0.6, **kw: Any) -> None:
        super().__init__(answer=f"{first} {rest}" if rest else first, **kw)
        self.first, self.rest, self.delay_rest = first, rest, delay
        self.warm: list[tuple[bool, str]] = []

    def keep_warm(self, on: bool, owner: str = "live") -> None:
        self.warm.append((on, owner))

    async def look(self, question: str, *, asked_by: str = "parent", on_first=None) -> dict[str, Any]:
        self.asked.append((question, asked_by))
        if on_first is not None:
            on_first(self.first)
        await asyncio.sleep(self.delay_rest)
        return {"ok": True, "answer": self.answer, "error": None, "detail": None, "latency_ms": 5,
                "source": "chatgpt", "first": self.first, "rest": self.rest}


async def test_a_look_starts_while_the_person_is_still_talking(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(delay=0.3)
    caplog.set_level(logging.INFO)
    await connect(svc, server)
    for piece in ("How many ", "fingers ", "am"):
        partial(server, piece)
    await asyncio.sleep(0.3)
    assert look.asked == []  # "how many fingers" waits for two more words (not "...does a cat have")
    partial(server, " I holding")
    await wait_for(lambda: look.asked)
    assert look.asked == [("How many fingers am I holding", "live")]  # before the person even finished
    hear(server, "How many fingers am I holding up?")
    delegate(server, "p1", "Look: how many fingers is the person holding up?")
    await wait_for(lambda: answers(server, "p1"))
    assert answers(server, "p1") == ["You are holding up three fingers."]
    await asyncio.sleep(1.2)
    assert len(look.asked) == 1 and svc.status()["photos"] == 1 and provider.calls == []
    assert notes(server) == []  # the hand-off got it: said once
    assert "look triggered (from=partial) -> ok in" in caplog.text
    assert "the partial look started" in caplog.text


async def test_cumulative_partial_transcripts_work_too(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    for text in ("what", "what colour", "what colour is this"):
        partial(server, text)
    await wait_for(lambda: look.asked)
    assert look.asked == [("what colour is this", "live")]
    await wait_for(lambda: notes(server))  # nobody handed it over: the voice is told
    assert notes(server)[0].startswith("You just looked through your camera.")


async def test_partial_words_that_are_not_about_seeing_take_no_photo(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    for piece in ("How many ", "fingers ", "does a ", "cat have?"):
        partial(server, piece)
    hear(server, "How many fingers does a cat have?")
    partial(server, "see you ")
    partial(server, "later")
    await asyncio.sleep(0.5)
    assert look.asked == []


async def test_the_first_sentence_answers_the_hand_off_and_the_rest_follows(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = StreamingLook(delay=0.8)
    await connect(svc, server)
    delegate(server, "f1", "Look: what is the person holding?")
    await wait_for(lambda: answers(server, "f1"), timeout=0.7)  # well before the whole answer
    assert answers(server, "f1") == ["A red ball!"]
    await wait_for(lambda: notes(server))
    assert notes(server) == ["More of what your camera saw: It is on the green rug."]
    assert len(look.asked) == 1


async def test_a_one_sentence_answer_has_no_follow_up(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = StreamingLook(first="Three fingers!", rest=None, delay=0.2)
    await connect(svc, server)
    delegate(server, "f2", "Look: how many fingers?")
    await wait_for(lambda: answers(server, "f2"))
    await asyncio.sleep(0.6)
    assert answers(server, "f2") == ["Three fingers!"] and notes(server) == [] and look.asked


async def test_the_camera_is_kept_warm_only_during_the_call(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = StreamingLook()
    await connect(svc, server)
    assert look.warm == [(True, "live_voice")]
    await svc.stop_session()
    assert look.warm == [(True, "live_voice"), (False, "live_voice")]


async def test_a_robot_counted_answer_is_captioned_as_such(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live

    class CountingLook(FakeLook):
        async def look(self, question: str, *, asked_by: str = "parent", on_first=None) -> dict[str, Any]:
            self.asked.append((question, asked_by))
            if on_first is not None:
                on_first("I see two fingers held up!")
            return {"ok": True, "answer": "I see two fingers held up!", "error": None, "detail": None,
                    "latency_ms": 90, "source": "robot", "first": "I see two fingers held up!", "rest": None}

    ctx.extras["camera_look"] = CountingLook()
    await connect(svc, server)
    delegate(server, "r1", "Look: how many fingers?")
    await wait_for(lambda: answers(server, "r1"))
    assert answers(server, "r1") == ["I see two fingers held up!"]
    assert "Camera answer (counted on the robot; the photo was not sent): I see two fingers held up!" \
        in note_captions(svc)


def test_seeing_words_while_still_talking():
    from littlebot.chatgpt.live import asks_to_see

    for text in ("how many fingers am I", "how many fingers do you see", "what am I holding", "can you see me",
                 "look at this", "what colour is this", "How many fingers is daddy holding"):
        assert asks_to_see(text, partial=True), text
    for text in ("how many fingers", "how many fingers am", "how many fingers does", "how many fingers does a",
                 "how many fingers do you", "see you later", "I like to look up"):
        assert not asks_to_see(text, partial=True), text
    assert asks_to_see("how many fingers")  # a finished sentence that says just that is a clear ask
    # a short whole-sentence question waits for its sentence to end ("what is this song" is not one)
    assert not asks_to_see("what is this", partial=True) and not asks_to_see("what is this song", partial=True)
    assert asks_to_see("what is this?", partial=True) and asks_to_see("Oh, what is this? I found it", partial=True)
    assert not asks_to_see("and now", partial=True, follow_up=True)
    assert asks_to_see("and now?", partial=True, follow_up=True)


# --------------------------------------------------------------------------- telling the voice sooner, once
def voice_says(server: FakeOpenAI, text: str) -> None:
    server.send({"type": "output_transcript.added", "item": {"text": text}})


def log_line(caplog: Any, needle: str) -> str:
    return next(r.getMessage() for r in caplog.records if needle in r.getMessage())


async def test_without_a_hand_off_a_quick_answer_is_told_soon_after_their_words(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()  # answers at once, like a finger count on the robot
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    await connect(svc, server)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    hear(server, "How many fingers am I holding up?")
    await wait_for(lambda: notes(server))
    took = loop.time() - t0
    assert live_mod.NOTE_GRACE_S - 0.05 <= took < 0.95  # held 1 s from the look's start before
    assert len(look.asked) == 1
    line = log_line(caplog, "look answer told via note")
    assert "ready at +" in line and f"no hand-off {live_mod.NOTE_GRACE_S:g} s after their last words" in line


async def test_no_note_while_they_are_still_talking(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    partial(server, "How many fingers am I holding")
    await wait_for(lambda: look.asked)
    await asyncio.sleep(1.3)  # the answer is long ready, but they haven't finished: don't talk over them
    assert notes(server) == []
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    hear(server, "How many fingers am I holding up right now?")
    await wait_for(lambda: notes(server))
    assert loop.time() - t0 < 0.95 and len(look.asked) == 1


async def test_the_voice_talking_without_a_hand_off_gets_the_answer_at_once(live, ctx, caplog, monkeypatch):
    pytest.importorskip("littlebot.chatgpt.look")
    monkeypatch.setattr(live_mod, "NOTE_GRACE_S", 5.0)
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    await connect(svc, server)
    hear(server, "What am I holding?")
    await wait_for(lambda: look.asked)
    await asyncio.sleep(0.3)
    assert notes(server) == []  # a hand-off may still come
    voice_says(server, "Ooh, let me ")  # it answers on its own instead
    await wait_for(lambda: notes(server), timeout=1.0)
    assert notes(server)[0].startswith("You just looked through your camera.")
    assert "the voice talked without handing it over" in log_line(caplog, "look answer told via note")


async def test_a_hand_off_on_its_way_still_gets_the_answer_and_no_note_follows(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    await connect(svc, server)
    hear(server, "How many fingers am I holding up?")
    await wait_for(lambda: look.asked)
    await asyncio.sleep(0.2)
    delegate(server, "w1", "Look: how many fingers is the person holding up?")
    await wait_for(lambda: answers(server, "w1"))
    assert answers(server, "w1") == ["You are holding up three fingers."]  # no "you were told" when it wasn't
    await asyncio.sleep(live_mod.NOTE_GRACE_S + 0.3)
    assert notes(server) == [] and len(look.asked) == 1
    line = log_line(caplog, "look answer told via hand-off")
    assert "the hand-off came at +" in line and "held" in line


async def test_seeing_hand_offs_never_go_to_the_tools_or_the_text_model(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    provider.supports_tools = True  # tools on: "game", "video" and "activity" hand-offs go to them

    async def reply(messages, *, system, max_tokens, timeout, **kw):
        provider.calls.append({"messages": messages, "tools": bool(kw.get("tools"))})
        return ProviderReply(text="Okay!", model="fake")

    provider.reply = reply  # type: ignore[method-assign]
    look = ctx.extras["camera_look"] = FakeLook(answer="A puzzle box!")
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    await connect(svc, server)
    delegate(server, "g1", "Look: what game is the child holding?")  # names a game, but asks to look
    await wait_for(lambda: answers(server, "g1"))
    assert answers(server, "g1") == ["A puzzle box!"] and provider.calls == []
    delegate(server, "g2", "Tell me what this is")  # no look / see words at all
    await wait_for(lambda: answers(server, "g2"))
    assert answers(server, "g2") == ["A puzzle box!"] and provider.calls == [] and len(look.asked) == 2
    delegate(server, "g3", "Show me a video of a dog")  # asks for something to be done: the tools
    await wait_for(lambda: answers(server, "g3"))
    delegate(server, "g4", "Look for a dinosaur video and play it")
    await wait_for(lambda: answers(server, "g4"))
    assert len(look.asked) == 2 and [c["tools"] for c in provider.calls] == [True, True]
    assert caplog.text.count("hand-off received (kind=look)") == 2
    assert caplog.text.count("hand-off received (kind=control)") == 2


async def test_a_vague_hand_off_during_the_look_their_words_asked_for_gets_that_look(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(answer="A red ball!", delay=0.3)
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    await connect(svc, server)
    hear(server, "What is this?")
    delegate(server, "v1", "Describe that thing for them")  # no seeing words: the text model would guess
    await wait_for(lambda: answers(server, "v1"))
    assert answers(server, "v1") == ["A red ball!"] and provider.calls == [] and len(look.asked) == 1
    assert "hand-off received (kind=look, from the person's words)" in caplog.text
    delegate(server, "v2", "What is the capital of France?")
    await wait_for(lambda: answers(server, "v2"))
    assert answers(server, "v2") == ["A cow says moo."] and len(look.asked) == 1


async def test_the_look_log_says_where_the_time_went(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live

    class TimedLook(FakeLook):
        async def look(self, question: str, *, asked_by: str = "parent", on_first=None) -> dict[str, Any]:
            out = await super().look(question, asked_by=asked_by)
            return {**out, "timing": {"photo": "warm", "photo_age_ms": 120, "photo_ms": 0, "count": "unsure:fist",
                                      "count_ms": 90, "detail": "high", "first_sentence_ms": 640, "chatgpt_ms": 800}}

    ctx.extras["camera_look"] = TimedLook()
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    await connect(svc, server)
    partial(server, "how many fingers am I holding")
    hear(server, "how many fingers am I holding up?")
    delegate(server, "t1", "Look: how many fingers?")
    await wait_for(lambda: answers(server, "t1"))
    line = log_line(caplog, "look triggered (from=partial) -> ok in")
    assert line.endswith("| photo=warm photo_age_ms=120 photo_ms=0 count=unsure:fist count_ms=90 detail=high "
                         "first_sentence_ms=640 chatgpt_ms=800")
    assert "look answer told via hand-off" in caplog.text
    assert "fingers" not in caplog.text and "holding" not in caplog.text  # labels and numbers only


async def test_and_now_right_after_a_look_looks_again_before_the_hand_off(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    hear(server, "How many fingers am I holding up?")
    await wait_for(lambda: notes(server))
    partial(server, "And now?")  # a finger game: the next round
    await wait_for(lambda: len(look.asked) == 2)
    hear(server, "And now?")
    delegate(server, "n1", "Look again: how many fingers now?")
    await wait_for(lambda: answers(server, "n1"))
    assert answers(server, "n1") == ["You are holding up three fingers."]
    await asyncio.sleep(0.3)
    assert len(look.asked) == 2 and svc.status()["photos"] == 2  # one photo per question


async def test_a_new_question_soon_after_a_look_starts_its_own_look_early(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    hear(server, "What am I holding?")
    await wait_for(lambda: notes(server))
    partial(server, "And what colour is my ")  # within LOOK_REUSE_S, but a new question: look while they talk
    await wait_for(lambda: len(look.asked) == 2)
    hear(server, "And what colour is my shirt?")  # its own words don't look a third time
    await asyncio.sleep(0.3)
    assert len(look.asked) == 2


async def test_late_words_after_a_hand_off_take_no_second_photo(live, ctx):
    pytest.importorskip("littlebot.chatgpt.look")
    svc, server, _audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook()
    await connect(svc, server)
    delegate(server, "late1", "Look: how many fingers is the person holding up?")
    await wait_for(lambda: answers(server, "late1"))
    partial(server, "How many fingers am I holding")  # their transcript lags behind the voice
    hear(server, "How many fingers am I holding up?")
    await asyncio.sleep(0.8)
    assert len(look.asked) == 1 and notes(server) == []


async def test_the_voice_is_kept_quiet_while_it_looks_and_heard_again_with_the_answer(live, ctx, caplog):
    pytest.importorskip("littlebot.chatgpt.look")
    caplog.set_level(logging.INFO, logger="littlebot.chatgpt.live")
    svc, server, audio, _provider, _alerts = live
    look = ctx.extras["camera_look"] = FakeLook(delay=0.4)
    await connect(svc, server)
    await wait_for(lambda: audio.frames > 5)
    hear(server, "How many fingers am I holding up?")
    await asyncio.sleep(0.15)  # frames already on their way
    held_from = audio.frames
    await asyncio.sleep(0.2)  # the "voice" talks ("One second..."), but isn't heard
    assert audio.frames - held_from <= 1 and look.asked
    await wait_for(lambda: notes(server))  # the answer is on its way to the voice ...
    await asyncio.sleep(0.2)
    assert audio.frames - held_from <= 2  # ... but it is still mid-word: not cut in
    server.voice_quiet = True  # it pauses
    await asyncio.sleep(0.3)
    server.voice_quiet = False
    await wait_for(lambda: audio.frames - held_from > 10)  # and is heard again: the answer
    assert "the voice is heard again" in caplog.text and "too long" not in caplog.text


async def test_the_voice_is_never_held_back_for_long(live, ctx, monkeypatch):
    pytest.importorskip("littlebot.chatgpt.look")
    from littlebot.chatgpt import live as live_mod

    monkeypatch.setattr(live_mod, "HUSH_MAX_S", 0.3)
    svc, server, audio, _provider, _alerts = live
    ctx.extras["camera_look"] = FakeLook(delay=5.0)  # a look that takes far too long
    await connect(svc, server)
    hear(server, "How many fingers am I holding up?")
    await asyncio.sleep(0.2)
    held_from = audio.frames
    await wait_for(lambda: audio.frames - held_from > 10)


@pytest.mark.parametrize("words", ["Stop listening.", "Stop talking!", "Never mind", "I wasn't talking to you.",
                                   "Okay, bye bye!"])
async def test_saying_stop_ends_the_call(live, words):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    hear(server, words)
    await wait_for(lambda: not svc.status()["active"])
    assert svc.status()["last_call"]["ended_reason"] == "asked to stop"


async def test_stop_inside_a_sentence_doesnt_end_the_call(live):
    svc, server, _audio, _provider, _alerts = live
    await connect(svc, server)
    hear(server, "Can you stop the video?")
    hear(server, "Let's play stop and go.")
    await asyncio.sleep(0.2)
    assert svc.status()["active"]
