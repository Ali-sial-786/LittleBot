"""Live voice: hand-offs answered with LittleBot's tools, the 5 s interim answer, and the hand-over
to a video / activity / sleep after the call (local WebRTC peer standing in for OpenAI)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, signed_in_auth  # noqa: E402

import littlebot.chatgpt.live as live_mod  # noqa: E402
from littlebot.brain.providers.base import BaseProvider, ProviderReply  # noqa: E402
from littlebot.chatgpt.live import LiveVoiceService  # noqa: E402
from littlebot.core.arbiter import Resource  # noqa: E402
from littlebot.core.events import Topics  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class LendingAudio:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        self.calls.append(f"suspend:{owner}")

    def resume(self, owner: str) -> None:
        self.calls.append(f"resume:{owner}")

    def status(self) -> dict[str, Any]:
        return {"volume": 0.5}


class ToolProvider(BaseProvider):
    """The text model: runs a scripted list of tool calls, then answers."""

    name = "chatgpt"
    is_cloud = True
    model = "fake"
    supports_tools = True

    def __init__(self, script: list[tuple[str, dict[str, Any]]] | None = None, text: str = "Okay!",
                 delay: float = 0.0) -> None:
        self.script, self.text, self.delay = script or [], text, delay
        self.calls: list[dict[str, Any]] = []
        self.results: list[str] = []

    async def reply(self, messages: list[dict[str, str]], *, system: str, max_tokens: int, timeout: float,
                    tools: list[dict[str, Any]] | None = None, run_tool: Any = None, max_tool_rounds: int = 6,
                    images: Any = None) -> ProviderReply:
        self.calls.append({"messages": messages, "system": system, "tools": [t["name"] for t in tools or []]})
        if self.delay:
            await asyncio.sleep(self.delay)
        for name, args in self.script:
            self.results.append(await run_tool(name, json.dumps(args)))
        return ProviderReply(text=self.text, model="fake")


class FakeActivities:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.started: list[tuple[str, str]] = []
        self.speaker_holder_at_start: list[Any] = []

    def list(self, age_months: int | None = None, category: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "peekaboo", "name": "Peekaboo", "category": "social", "enabled": True, "tags": []}]

    async def start(self, activity_id: str, *, source: str = "dashboard") -> dict[str, Any]:
        holder = self.ctx.arbiter.holder(Resource.AUDIO_OUT)
        self.speaker_holder_at_start.append(holder.owner if holder else None)
        self.started.append((activity_id, source))
        return {}

    async def stop(self, reason: str = "stopped") -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"running": False}


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
def make_live(ctx, tmp_path):
    """make_live(provider) -> a connected call (each test closes it with ``close``)."""

    async def _make(provider: BaseProvider) -> tuple[LiveVoiceService, FakeOpenAI, FakeLiveAudio]:
        ctx.privacy.set(cloud_enabled=True)
        ctx.audio = LendingAudio()
        server, audio = FakeOpenAI(), FakeLiveAudio()
        svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                               audio_factory=lambda: audio, provider=provider)
        await svc.start()
        result = await svc.start_session(parent_present=True)
        assert result["ok"], result
        await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")
        return svc, server, audio

    return _make


async def close(svc: LiveVoiceService, server: FakeOpenAI) -> None:
    await svc.stop()
    await server.close()


def delegate(server: FakeOpenAI, did: str, text: str) -> None:
    server.send({"type": "delegation.created", "item": {"id": did, "type": "delegation", "target": "client",
                                                        "content": [{"type": "input_text", "text": text}]}})


def answers(server: FakeOpenAI, did: str) -> list[str]:
    return [m["content"][0]["text"] for m in server.received
            if m.get("type") == "delegation.context.append" and m.get("delegation_item_id") == did]


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


async def test_the_voice_is_told_it_can_control_the_robot(ctx, make_live):
    svc, server, _audio = await make_live(ToolProvider())
    try:
        instructions = server.requests[0]["body"]["session"]["instructions"]
        for words in ("turn the volume up", "start the peekaboo activity", "play a baby shark song video",
                      "how hot is the robot", "LittleBot can look things up", "Never say you can't check the weather"):
            assert words in instructions
        assert "searching online" not in instructions and "Never say the words YouTube" not in instructions
    finally:
        await close(svc, server)


async def test_volume_hand_off_is_done_with_the_tools(ctx, make_live):
    provider = ToolProvider([("volume_up", {})], text="It's louder now!")
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "v1", "Turn the volume up")
        await wait_for(lambda: answers(server, "v1"))
        assert answers(server, "v1") == ["It's louder now!"]
        assert ctx.settings.get("audio.volume") == pytest.approx(0.6)  # the live call plays at this volume
        call = provider.calls[0]
        assert "volume_up" in call["tools"] and "look_through_camera" not in call["tools"]
        assert "Things you can do (tools)" in call["system"] and "use your tools" in call["system"]
        assert call["messages"][-1] == {"role": "user", "content": "Turn the volume up"}
        assert svc.status()["active"]
    finally:
        await close(svc, server)


async def test_an_activity_starts_after_the_voice_said_goodbye_and_the_call_ended(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 0.5)  # the fake voice never "speaks"
    ctx.activities = acts = FakeActivities(ctx)
    provider = ToolProvider([("start_activity", {"activity": "peekaboo"})], text="Let's play peekaboo!")
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "a1", "Start the peekaboo activity")
        await wait_for(lambda: answers(server, "a1"))
        (text,) = answers(server, "a1")
        assert text.startswith("Let's play peekaboo!") and "this voice chat ends and the activity starts" in text
        assert not ctx.face.called("close_eyes")  # not taken for the peekaboo face
        assert acts.started == []  # not while the call holds the speaker
        await wait_for(lambda: not svc.status()["active"] and acts.started)
        last = svc.status()["last_call"]
        assert last["ended_reason"] == live_mod.HANDOVER_REASON and last["handed_over"] == ["activity peekaboo"]
        assert acts.started == [("peekaboo", "chatgpt")] and acts.speaker_holder_at_start == [None]
        assert ctx.audio.calls == ["suspend:live voice", "resume:live voice"]
    finally:
        await close(svc, server)


async def test_the_hand_over_waits_for_the_voice_to_finish(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 30.0)
    provider = ToolProvider([("go_to_sleep", {})], text="Night night!")
    svc, server, audio = await make_live(provider)
    try:
        delegate(server, "s1", "go to sleep")
        await wait_for(lambda: answers(server, "s1"))
        assert "LittleBot goes to sleep" in answers(server, "s1")[0]
        await asyncio.sleep(0.3)
        assert svc.status()["active"]  # the voice hasn't said it yet
        audio.speaking = True
        await asyncio.sleep(0.3)
        audio.speaking = False
        await wait_for(lambda: not svc.status()["active"], timeout=5)
        await wait_for(lambda: ctx.state.state == RobotState.SLEEPING, timeout=5)
        assert svc.status()["last_call"]["handed_over"] == ["sleep"]
    finally:
        await close(svc, server)


async def test_stop_all_cancels_the_hand_over(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 30.0)
    ctx.activities = acts = FakeActivities(ctx)
    svc, server, _audio = await make_live(ToolProvider([("start_activity", {"activity": "peekaboo"})]))
    try:
        delegate(server, "a2", "start the peekaboo activity")
        await wait_for(lambda: answers(server, "a2"))
        ctx.bus.publish(Topics.STOP_ALL, {"source": "test"})
        await wait_for(lambda: not svc.status()["active"])
        await asyncio.sleep(0.2)
        assert acts.started == [] and "handed_over" not in svc.status()["last_call"]
    finally:
        await close(svc, server)


async def test_a_slow_answer_gets_an_interim_reply_then_a_note(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOFF_INTERIM_S", 0.2)
    monkeypatch.setattr(live_mod, "HANDOFF_TIMEOUT_S", 5.0)
    svc, server, _audio = await make_live(ToolProvider(text="The robot is 48 degrees, nice and cool.", delay=0.8))
    try:
        delegate(server, "t1", "How hot is the robot?")
        await wait_for(lambda: answers(server, "t1"))
        assert answers(server, "t1") == [live_mod.STILL_WORKING]  # within the interim time, not silence
        assert notes(server) == []
        await wait_for(lambda: notes(server))
        assert notes(server) == [live_mod.LATE_ANSWER.format(answer="The robot is 48 degrees, nice and cool.")]
        assert answers(server, "t1") == [live_mod.STILL_WORKING]  # a hand-off is answered once
    finally:
        await close(svc, server)


async def test_an_answer_that_never_comes_ends_with_a_short_note(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOFF_INTERIM_S", 0.2)
    monkeypatch.setattr(live_mod, "HANDOFF_TIMEOUT_S", 0.6)
    svc, server, _audio = await make_live(ToolProvider(delay=60))
    try:
        delegate(server, "t2", "What can the robot do?")
        await wait_for(lambda: notes(server), timeout=5)
        assert answers(server, "t2") == [live_mod.STILL_WORKING]
        assert notes(server) == [live_mod.LATE_FAILED]
        assert svc.status()["active"]
    finally:
        await close(svc, server)


async def test_a_quick_answer_has_no_interim_reply(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOFF_INTERIM_S", 2.0)
    svc, server, _audio = await make_live(ToolProvider(text="I can sing, dance and make faces!", delay=0.1))
    try:
        delegate(server, "q1", "what can the robot do")
        await wait_for(lambda: answers(server, "q1"))
        await asyncio.sleep(0.3)
        assert answers(server, "q1") == ["I can sing, dance and make faces!"] and notes(server) == []
    finally:
        await close(svc, server)


async def test_a_slow_take_over_is_announced_in_the_note_then_handed_over(ctx, make_live, monkeypatch):
    monkeypatch.setattr(live_mod, "HANDOFF_INTERIM_S", 0.2)
    monkeypatch.setattr(live_mod, "HANDOVER_MAX_S", 0.5)
    ctx.activities = acts = FakeActivities(ctx)
    provider = ToolProvider([("start_activity", {"activity": "peekaboo"})], text="Peekaboo time!", delay=0.6)
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "a3", "start the peekaboo activity")
        await wait_for(lambda: notes(server))
        assert answers(server, "a3") == [live_mod.STILL_WORKING]
        assert "Peekaboo time!" in notes(server)[0] and "this voice chat ends" in notes(server)[0]
        await wait_for(lambda: acts.started)
        assert svc.status()["last_call"]["ended_reason"] == live_mod.HANDOVER_REASON
    finally:
        await close(svc, server)


async def test_asking_for_an_activity_in_words_does_not_play_the_face(ctx, make_live):
    svc, server, _audio = await make_live(ToolProvider())
    try:
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "start the peekaboo activity"}})
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "make a happy face"}})
        await wait_for(lambda: ctx.face.expression == "happy")
        assert not ctx.face.called("close_eyes")
    finally:
        await close(svc, server)


async def test_without_tools_hand_offs_work_as_before(ctx, make_live):
    ctx.config.chatgpt.tools_enabled = False
    provider = ToolProvider(text="A cow says moo.")
    svc, server, _audio = await make_live(provider)
    try:
        delegate(server, "n1", "Start the peekaboo activity")  # a face request again
        await wait_for(lambda: answers(server, "n1"))
        assert answers(server, "n1")[0].startswith("Done: LittleBot is showing peekaboo")
        delegate(server, "n2", "What sound does a cow make?")
        await wait_for(lambda: answers(server, "n2"))
        assert answers(server, "n2") == ["A cow says moo."] and provider.calls[0]["tools"] == []
    finally:
        await close(svc, server)
