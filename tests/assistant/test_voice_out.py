"""VoiceOut: sentences to the speaker in order, with the voice's quiet start cut."""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from littlebot.assistant import voice_out as vo
from littlebot.assistant.sentences import Sentence

RATE = 48000


class Out:
    """A speaker that plays at once and keeps every sample."""

    queued_s = 0.0
    speaking = False

    def __init__(self) -> None:
        self.pcm: list[np.ndarray] = []

    def play_pcm(self, samples: np.ndarray, rate: int = RATE) -> None:
        self.pcm.append(np.asarray(samples, dtype=np.float32))

    def all(self) -> np.ndarray:
        return np.concatenate(self.pcm) if self.pcm else np.zeros(0, dtype=np.float32)


class Voice:
    """Each sentence: ``quiet_s`` of near-silence (as Cartesia sends it, in 40 ms pieces), then 0.3 s of voice."""

    sample_rate = RATE
    last_provider = "cartesia"

    def __init__(self, quiet_s: float, voiced_s: float = 0.3) -> None:
        self.quiet_s = quiet_s
        self.voiced_s = voiced_s

    async def stream(self, text: str) -> Any:
        quiet = np.full(int(RATE * self.quiet_s), 0.005, dtype=np.float32)
        voice = np.full(int(RATE * self.voiced_s), 0.3, dtype=np.float32)
        x = np.concatenate([quiet, voice])
        for i in range(0, len(x), 1920):
            await asyncio.sleep(0)
            yield x[i:i + 1920]


def _loud_start(x: np.ndarray) -> float:
    return int(np.flatnonzero(np.abs(x) > vo.VOICE_LEVEL)[0]) / RATE


async def test_the_quiet_start_of_each_sentence_is_cut() -> None:
    # Cartesia started "Let me check." with 0.58 s of near-silence on the robot: heard as a wait for the first word.
    out = Out()
    voice = vo.VoiceOut(Voice(quiet_s=0.6), out)
    voice.say(Sentence("Let me check."))
    voice.say(Sentence("Tomorrow will be cloudy."))
    voice.close()
    assert await voice.done()
    x = out.all()
    assert abs(_loud_start(x) - vo.LEAD_FIRST_S) < 0.01  # the first word comes almost at once
    second = x[round(RATE * (vo.LEAD_FIRST_S + 0.3)):]  # after the first sentence's voice
    assert abs(_loud_start(second) - vo.LEAD_NEXT_S) < 0.01  # a short natural pause between sentences
    assert len(x) / RATE == np.round(vo.LEAD_FIRST_S + 0.3 + vo.LEAD_NEXT_S + 0.3, 3)
    assert abs(voice.cut_s - (0.6 - vo.LEAD_FIRST_S + 0.6 - vo.LEAD_NEXT_S)) < 0.01
    assert voice.heard == ["Let me check.", "Tomorrow will be cloudy."]


async def test_a_sentence_that_starts_loud_or_is_all_quiet_still_plays() -> None:
    out = Out()
    voice = vo.VoiceOut(Voice(quiet_s=0.0), out)
    voice.say(Sentence("Hi."))
    voice.close()
    assert await voice.done()
    assert len(out.all()) == int(RATE * 0.3) and voice.cut_s == 0.0

    out = Out()
    voice = vo.VoiceOut(Voice(quiet_s=0.5, voiced_s=0.0), out)  # never louder than a whisper
    voice.say(Sentence("..."))
    voice.close()
    assert await voice.done()
    assert len(out.all()) == int(RATE * vo.LEAD_FIRST_S) and voice.heard == ["..."]


async def test_never_more_than_max_cut_is_dropped() -> None:
    out = Out()
    voice = vo.VoiceOut(Voice(quiet_s=vo.MAX_CUT_S + 1.0), out)
    voice.say(Sentence("A long breath first."))
    voice.close()
    assert await voice.done()
    assert voice.cut_s <= vo.MAX_CUT_S + 0.05
    assert len(out.all()) / RATE >= 1.0 + 0.3 - 0.05
