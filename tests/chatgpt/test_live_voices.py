"""Live voice knowing who is talking, by voice (littlebot/voices/call.py), through a real local call.

The fake model (littlebot/testing.py: speaker k is a steady tone) stands in for sherpa-onnx; the fake
microphone "talks" as one speaker or another. The far end is the local WebRTC peer of test_live.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import numpy as np
import pytest

pytest.importorskip("aiortc")

from chatgpt_helpers import FakeLiveAudio, FakeOpenAI, FakeProvider, signed_in_auth  # noqa: E402

from littlebot.audio.carry import SpeechCarry  # noqa: E402
from littlebot.chatgpt.live import LiveVoiceService  # noqa: E402
from littlebot.core.events import Topics  # noqa: E402
from littlebot.testing import SPEAKER_HZ, FakeSpeakerEngine, speaker_tone  # noqa: E402
from littlebot.voices import call as C  # noqa: E402
from littlebot.voices.service import VoiceRecognition  # noqa: E402

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


class TalkingAudio(FakeLiveAudio):
    """The call's microphone: ``speaker`` (a fake speaker's tone) talking, or silence while it is None."""

    def __init__(self) -> None:
        super().__init__()
        self.speaker: int | None = None
        self.read = 0
        self._n = 0

    async def read_frame(self) -> np.ndarray:
        await asyncio.sleep(0.02)
        self.read += 1
        if self.speaker is None:
            return np.zeros(960, dtype=np.int16)
        t = (np.arange(960) + self._n) / 48000
        self._n += 960
        return (np.sin(2 * np.pi * SPEAKER_HZ[self.speaker] * t) * 6000).astype(np.int16)


async def wait_for(cond: Any, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


@pytest.fixture
async def call(ctx, tmp_path):
    """A live voice service with voices on: Ali (dad) is fake speaker 0, Sara (mum) 1, Amara (the baby) 2."""
    ctx.privacy.set(cloud_enabled=True, voice_recognition_enabled=True)
    ctx.audio = LendingAudio()
    ctx.settings.set("profile", {"nickname": "Amara", "age_months": 10})
    engine = FakeSpeakerEngine()
    voices = ctx.voices = VoiceRecognition(ctx, engine=engine)
    await voices.start()
    ids = {}
    for name, role, k in (("Ali", "dad", 0), ("Sara", "mum", 1), ("Amara", "the baby", 2)):
        ids[name] = voices.store.people.add_person(name, role)["id"]
        assert (await voices.enrol(ids[name], speaker_tone(k, 12.0, 48000, int16=True), 48000))["ok"]
    server = FakeOpenAI()
    audio = TalkingAudio()
    svc = LiveVoiceService(ctx, auth=signed_in_auth(tmp_path), transport=httpx.MockTransport(server),
                           audio_factory=lambda: audio, provider=FakeProvider())
    await svc.start()
    events: list[dict[str, Any]] = []
    ctx.bus.subscribe(Topics.VOICE_SPEAKER, lambda e: events.append(dict(e.data)))
    yield svc, server, audio, voices, ids, events, engine
    await svc.stop()
    await server.close()
    await voices.stop()


async def connect(svc: LiveVoiceService, server: FakeOpenAI, carry: Any = None) -> None:
    result = await svc.start_session(parent_present=True, carry=carry)
    assert result["ok"], result
    await wait_for(lambda: server.channel_ready.is_set() and svc.status().get("phase") == "listening")


def instructions(server: FakeOpenAI) -> str:
    return server.requests[-1]["body"]["session"]["instructions"]


def notes(server: FakeOpenAI) -> list[str]:
    return [m["content"][0]["text"] for m in server.received if m.get("type") == "session.context.append"]


def voice_notes(server: FakeOpenAI) -> list[str]:
    return [n for n in notes(server) if n.startswith("By voice")]


def hear(server: FakeOpenAI, text: str) -> None:
    server.send({"type": "turn.done", "turn": {"role": "user", "transcript": text}})


async def talk(server: FakeOpenAI, audio: TalkingAudio, speaker: int, text: str, seconds: float = 2.0) -> None:
    """``speaker`` says ``text`` for about ``seconds``; then their turn ends (ChatGPT's transcript)."""
    audio.speaker = speaker
    start = audio.read
    await wait_for(lambda: audio.read - start >= seconds * 50)
    audio.speaker = None
    hear(server, text)


def wake_carry(speaker: int) -> SpeechCarry:
    """"Hey LittleBot, what's the weather?" said by ``speaker`` (the wake word kept the phrase for voices)."""
    c = SpeechCarry(16000, quiet_level=0.0105, keep_voice=True)
    c.keep_wake_phrase([speaker_tone(speaker, 1.0)])
    c.add(speaker_tone(speaker, 0.8))
    c.add(np.zeros(8000, np.float32))
    c.seal("live voice took the microphone")
    return c


async def test_the_voice_knows_who_woke_it_from_the_start(call, caplog):
    caplog.set_level(logging.INFO)
    svc, server, _audio, _voices, ids, events, _engine = call
    await connect(svc, server, wake_carry(0))
    text = instructions(server)
    assert "The grown-up who woke you sounds like Ali (dad), by voice." in text
    assert C.VOICES_INTRO in text
    assert events[0]["source"] == "wake" and events[0]["person_id"] == ids["Ali"]
    assert "the wake phrase was recognised by voice" in caplog.text and "Ali" not in caplog.text
    await asyncio.sleep(0.3)
    assert voice_notes(server) == []  # already in the instructions


async def test_each_turn_is_published_and_a_new_speaker_is_one_quiet_note(call):
    svc, server, audio, _voices, ids, events, _engine = call
    await connect(svc, server)
    assert C.VOICES_INTRO in instructions(server) and "woke you" not in instructions(server)
    await talk(server, audio, 0, "Can we read a book?")
    await wait_for(lambda: voice_notes(server))
    assert voice_notes(server) == ["By voice, the grown-up talking now is Ali (dad). Don't announce this; just "
                                   "know it."]
    assert events[-1]["person_id"] == ids["Ali"] and events[-1]["source"] == "turn"
    assert set(events[-1]) == {"person_id", "name", "role", "is_child", "score", "speech_s", "source"}
    await talk(server, audio, 0, "The one about the duck.")
    await wait_for(lambda: len(events) >= 2)
    await talk(server, audio, 2, "ba ba ba")  # the baby: published, never a note
    await wait_for(lambda: len(events) >= 3)
    assert events[-1]["is_child"] is True and len(voice_notes(server)) == 1
    await talk(server, audio, 1, "Hello, what are you two doing?")
    await wait_for(lambda: len(voice_notes(server)) == 2)
    assert "Sara (mum)" in voice_notes(server)[-1]
    await talk(server, audio, 4, "Hi there!")  # a visitor: unsure, nothing said
    await wait_for(lambda: len(events) >= 5)
    assert events[-1]["person_id"] is None and events[-1]["is_child"] is None
    await asyncio.sleep(0.3)
    assert len(voice_notes(server)) == 2


async def test_im_ali_teaches_littlebot_their_voice(call):
    svc, server, audio, voices, _ids, _events, _engine = call
    sam = voices.store.people.add_person("Sam", "uncle")["id"]
    await connect(svc, server)
    await talk(server, audio, 3, "Hi LittleBot, I'm Sam.", seconds=3.0)
    await wait_for(lambda: voices.store.count(sam) == 1)
    await wait_for(lambda: any("will now recognise Sam's voice" in n for n in notes(server)))
    await talk(server, audio, 5, "I'm Zara.")
    await wait_for(lambda: any("Nobody called Zara" in n for n in notes(server)))


async def test_do_you_know_who_i_am_is_answered_by_voice(call):
    svc, server, audio, _voices, _ids, _events, _engine = call
    await connect(svc, server)
    await talk(server, audio, 1, "Do you know who I am?")
    await wait_for(lambda: any("recognises the person talking as Sara (mum)" in n for n in notes(server)))


async def test_switched_off_nothing_is_listened_to(call):
    svc, server, audio, voices, _ids, events, engine = call
    voices.ctx.privacy.set(voice_recognition_enabled=False)
    calls = engine.calls
    await connect(svc, server, wake_carry(0))
    assert C.VOICES_INTRO not in instructions(server) and "woke you" not in instructions(server)
    await talk(server, audio, 0, "Can we read a book?")
    await asyncio.sleep(0.5)
    assert events == [] and voice_notes(server) == [] and engine.calls == calls
    assert svc._call is None or svc._call._voices is None
