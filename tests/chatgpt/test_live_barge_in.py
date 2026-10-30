"""Barge-in in a live call (littlebot/chatgpt/live.py's hooks) against the local WebRTC peer standing in for OpenAI."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402

from littlebot.chatgpt.live import (  # noqa: E402
    BARGE_IN_GO_AHEAD,
    BARGE_IN_INSTRUCTION,
    BARGE_IN_ROOM_TALK,
    LiveVoiceService,
    _for_robot,
)
from littlebot.chatgpt.live_audio import is_go_ahead  # noqa: E402
from littlebot.core.events import Topics  # noqa: E402
from littlebot.core.state import RobotState  # noqa: E402

pytestmark = pytest.mark.timeout(60)


class BargeAudio(FakeLiveAudio):
    """LiveAudio's barge-in side, faked: on PipeWire's echo-cancelled devices."""

    barge_in_on = True

    def __init__(self) -> None:
        super().__init__()
        self.identify: Any = None
        self.on_change: Any = None
        self.cut_in = False
        self.heard: list[tuple[str, bool]] = []

    def setup_barge_in(self, *, identify: Any = None, on_change: Any = None) -> None:
        self.identify, self.on_change = identify, on_change

    def interrupt(self) -> None:  # what LiveAudio does when a grown-up talks over the voice
        self.cut_in, self.speaking = True, False
        self.on_change("interrupted", {"speech_s": 0.4, "pitch_hz": 120, "voice": None})

    def barge_in_heard(self, text: str, final: bool) -> str | None:
        self.heard.append((text, final))
        if final and self.cut_in:
            self.cut_in = False
            return "go ahead" if is_go_ahead(text) else None
        return None


class LendingAudio:
    async def suspend(self, owner: str, wait: float = 1.5) -> None:
        pass

    def resume(self, owner: str) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"volume": 0.5}


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


async def start_call(ctx: Any, tmp_path: Any, audio: Any) -> tuple[LiveVoiceService, FakeOpenAI]:
    ctx.privacy.set(cloud_enabled=True)
    ctx.audio = LendingAudio()
    server = FakeOpenAI()
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                           audio_factory=lambda: audio, provider=FakeProvider())
    await svc.start()
    result = await svc.start_session(parent_present=True)
    assert result["ok"], result
    await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")
    return svc, server


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


async def test_the_voice_is_told_about_interruptions_only_when_they_can_happen(ctx, tmp_path):
    svc, server = await start_call(ctx, tmp_path, FakeLiveAudio())  # USB devices: one at a time
    try:
        assert BARGE_IN_INSTRUCTION not in server.requests[0]["body"]["session"]["instructions"]
    finally:
        await svc.stop()
        await server.close()
    svc, server = await start_call(ctx, tmp_path, BargeAudio())
    try:
        assert BARGE_IN_INSTRUCTION in server.requests[0]["body"]["session"]["instructions"]
    finally:
        await svc.stop()
        await server.close()


async def test_cutting_in_listens_at_once_and_oops_go_ahead_carries_on(ctx, tmp_path):
    async def identify(samples: Any, rate: int) -> None:
        return None

    ctx.voices = type("Voices", (), {"identify": staticmethod(identify)})()
    audio = BargeAudio()
    svc, server = await start_call(ctx, tmp_path, audio)
    visemes: list[dict[str, Any]] = []
    ctx.bus.subscribe(Topics.AUDIO_VISEME, lambda e: visemes.append(dict(e.data)))
    try:
        assert audio.identify is identify  # voice recognition reaches the detector
        audio.speaking = True
        await wait_for(lambda: ctx.state.state == RobotState.SPEAKING)
        audio.interrupt()
        assert ctx.state.state == RobotState.LISTENING and svc.status()["phase"] == "listening"  # not at the next poll
        await wait_for(lambda: {"level": 0.0} in visemes)  # the mouth closes
        server.send({"type": "input_transcript.added", "item": {"text": "oops"}})
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "Oops, go ahead."}})
        await wait_for(lambda: BARGE_IN_GO_AHEAD in notes(server))
        assert audio.heard == [("oops", False), ("Oops, go ahead.", True)]
        # a real turn after cutting in: no note
        audio.interrupt()
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "What's a giraffe?"}})
        await wait_for(lambda: len(audio.heard) == 3)
        await asyncio.sleep(0.2)
        assert notes(server).count(BARGE_IN_GO_AHEAD) == 1
    finally:
        await svc.stop()
        await server.close()


async def test_a_stop_word_heard_on_the_robot_quiets_the_voice_without_waiting_for_their_caption(ctx, tmp_path):
    # on the robot (2026-10-29) a short "stop" got no caption from ChatGPT: a false alarm, the voice talked on
    from littlebot.chatgpt.live import QUIET_NOTE

    audio = BargeAudio()
    svc, server = await start_call(ctx, tmp_path, audio)
    try:
        audio.speaking = True
        await wait_for(lambda: ctx.state.state == RobotState.SPEAKING)
        audio.interrupt()
        audio.on_change("stop_word", {"said": "stop"})  # what LiveAudio sends next for a stop word
        assert audio.dropped == 1 and svc.status()["active"]
        await wait_for(lambda: QUIET_NOTE in notes(server))
    finally:
        await svc.stop()
        await server.close()


async def test_room_talk_over_the_voice_lets_it_carry_on(ctx, tmp_path):
    audio = BargeAudio()
    svc, server = await start_call(ctx, tmp_path, audio)
    try:
        audio.speaking = True
        audio.interrupt()
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "Yeah, same. I'm just putting in "
                                                                                  "the salt"}})  # from a real call
        await wait_for(lambda: BARGE_IN_ROOM_TALK in notes(server))
        audio.interrupt()
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "Can you show me the recipe?"}})
        await wait_for(lambda: len(audio.heard) == 2)
        server.send({"type": "turn.done", "turn": {"role": "user", "transcript": "We're leaving in two minutes"}})
        await wait_for(lambda: len(audio.heard) == 3)  # nobody cut in this time: an ordinary turn
        await asyncio.sleep(0.2)
        assert notes(server).count(BARGE_IN_ROOM_TALK) == 1
    finally:
        await svc.stop()
        await server.close()


@pytest.mark.parametrize(("words", "for_it"), [
    # from real calls, said over the voice
    ("Yeah, same. I'm just putting in the salt Ah, I don't like this. It makes it too much pressure", False),
    (". I thought cold anyway Yeah, yeah, let's not, it's not, leave, yeah, leave it. We're leaving in two minutes",
     False),
    ("Hassan, come here please", False),
    ("Why are you not showing it on the screen", True),
    ("Can you, can you show me the, uh, precipitation on an hourly basis for Hamilton", True),
    ("the weather by hour", False),  # a fragment: room talk to the rules (the decision model may know better)
    ("Show me the hourly weather", True),
    ("Hey LittleBot, are you there", True),
    ("Lil Bot, wait", True),
    ("No, that's fine.", True),
    ("Stop", True),
    ("Next step", True),
    ("Okay, what about tomorrow?", True),
    ("Ali, can you hold her for a second?", False),
])
def test_what_counts_as_said_to_littlebot(words, for_it):
    assert _for_robot(words, "LittleBot") is for_it
