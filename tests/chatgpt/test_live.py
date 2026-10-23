"""Live voice against a local WebRTC peer standing in for OpenAI (real aiortc call over this machine)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402

from littlebot.chatgpt.live import LiveVoiceService  # noqa: E402
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
    assert "toy robot" in provider.calls[0]["system"]
    assert {"who": "heard", "text": "what does a cow say"} in svc.status()["captions"]


async def test_unsafe_delegation_answer_is_replaced(live):
    svc, server, _audio, provider, _alerts = live
    provider.text = "Let's keep this a secret from your mommy."
    await connect(svc, server)
    server.send({"type": "delegation.created", "item": {"id": "d2", "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": "a question"}]}})
    await wait_for(lambda: any(m.get("type") == "delegation.context.append" for m in server.received))
    text = next(m for m in server.received if m.get("type") == "delegation.context.append")["content"][0]["text"]
    assert "secret" not in text and "grown-up" in text


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
                             "Tell them what you saw."]
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
    assert answers(server, "s2") == ["You are holding up three fingers."] and len(look.asked) == 1
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
                 "count my fingers", "What is she wearing?"):
        assert asks_to_see(text), text
    for text in ("See you later!", "nice to see you", "How many fingers does a cat have?",
                 "how many fingers do you have", "Do you see what I mean?", "What colour is the sky?",
                 "I like to look up at the clouds", "Make a happy face", "Let me see", ""):
        assert not asks_to_see(text), text


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
