"""Live voice against a local WebRTC peer standing in for OpenAI (real aiortc call over this machine)."""

from __future__ import annotations

import asyncio
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
