"""Lip-sync end to end: the assistant's voice through the real call audio (LiveAudio, a fake PortAudio whose
speaker callback runs in its own thread) moves the face's mouth: audio.level events with changing levels and
mouth shapes while LittleBot speaks, closed when it stops."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("av")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatgpt"))
from test_live_audio import DEVICES, FakeSD  # noqa: E402

from littlebot.assistant.agent import Brain  # noqa: E402
from littlebot.assistant.service import AssistantService  # noqa: E402
from littlebot.audio import devices as dev  # noqa: E402
from littlebot.chatgpt.live_audio import LiveAudio  # noqa: E402
from littlebot.core.events import Topics  # noqa: E402
from littlebot.testing import FakeSTT, make_test_context  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_session import Scripted, _wait  # noqa: E402


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audio"))
from test_audio_visemes import VOWELS, vowel  # noqa: E402


class SpeechLikeTTS:
    """Syllables: synthesised vowels (ah, ee, oo, eh, oh) with short pauses between them, 16 kHz."""

    name = "speechlike"
    sample_rate = 16000
    last_provider = "speechlike"

    async def stream(self, text: str) -> Any:
        gap = np.zeros(int(16000 * 0.08), dtype=np.float32)
        parts = []
        for v in ("ah", "ee", "oo", "eh", "oh", "ah", "ee"):
            parts += [vowel(VOWELS[v], seconds=0.22, natural=True), gap]
        voice = np.concatenate(parts)
        for i in range(0, len(voice), 1600):
            await asyncio.sleep(0)
            yield voice[i:i + 1600]

    async def warm(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {}


class SpeakerThread(threading.Thread):
    """PortAudio's output callback: 20 ms every 20 ms, in its own thread."""

    def __init__(self, audio: LiveAudio) -> None:
        super().__init__(daemon=True)
        self.audio = audio
        self.stop = threading.Event()

    def run(self) -> None:
        out = np.zeros((960, 2), dtype=np.float32)
        while not self.stop.is_set():
            self.audio._play_cb(out, 960, None, None)
            time.sleep(0.02)


async def test_the_mouth_follows_the_assistants_voice(monkeypatch) -> None:
    monkeypatch.setattr(dev, "query_devices", lambda: [dict(d) for d in DEVICES])
    audio = LiveAudio(mic="USB PnP", speaker="UACDemo", volume=lambda: 0.5, sd=FakeSD())
    speaker = SpeakerThread(audio)

    async def mic_frames() -> np.ndarray:  # the microphone: silence, 20 ms at a time
        await asyncio.sleep(0.02)
        return np.zeros(960, dtype=np.int16)

    audio.read_frame = mic_frames  # type: ignore[method-assign]
    audio.stalled = lambda: None  # type: ignore[method-assign]  # the fake microphone has no PortAudio callback
    ctx = make_test_context(assistant={"pipeline": "cascade", "follow_up_seconds": 1.0})
    ctx.privacy.set(cloud_enabled=True)
    stt = FakeSTT()
    svc = AssistantService(ctx, audio_factory=lambda: audio, stt_factory=lambda: stt, tts_factory=SpeechLikeTTS,
                           brain=Brain(ctx, provider=Scripted(["Here is a sentence for the mouth."])))
    ctx.assistant = svc
    frames: list[dict[str, Any]] = []
    ctx.bus.subscribe(Topics.AUDIO_VISEME, lambda e: frames.append(dict(e.data)))
    await svc.start()
    await svc.start_session()
    speaker.start()
    try:
        await _wait(lambda: stt.stream is not None)
        stt.say("say something")
        await _wait(lambda: not svc.status()["active"], timeout=8)
    finally:
        speaker.stop.set()
        speaker.join(1)
    loud = [f for f in frames if f["level"] >= 0.2]
    assert len(loud) >= 10  # the mouth opened many times while it spoke
    opened = [f["level"] >= 0.2 for f in frames]
    assert sum(1 for a, b in zip(opened, opened[1:]) if a != b) >= 6  # open and shut with the syllables
    assert len({f.get("shape") for f in loud} - {None, "closed"}) >= 2  # with real mouth shapes (ah, ee, oo ...)
    assert frames[-1]["level"] == 0.0  # closed at the end
