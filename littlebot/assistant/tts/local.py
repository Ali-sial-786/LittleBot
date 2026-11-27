"""The voice on the robot itself (no network, always there): the last voice of the chain.

It is LittleBot's existing local voice (littlebot/audio/tts): piper when ``audio.tts.provider`` is piper and its
model is installed, else espeak-ng, else the quiet mock babble, each trying the next if it fails. A sentence is
made whole in a worker thread (``asyncio.to_thread``; espeak takes ~0.1 s, piper ~0.5-1.5 s on a Pi 5) and handed
on in ~100 ms chunks at the voice's own rate (``sample_rate`` is set before the first chunk; the chain resamples).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import numpy as np

from littlebot.assistant.speech import SpeechError
from littlebot.audio.errors import AudioError
from littlebot.audio.tts import EspeakTTS, MockTTS, PiperTTS, TTSProvider

log = logging.getLogger("littlebot.assistant.tts.local")

CHUNK_SECONDS = 0.1


def local_voices(tts_cfg: Any) -> list[TTSProvider]:
    """``audio.tts`` -> the local voices to try, best first (the mock babble always last)."""
    provider = getattr(tts_cfg, "provider", "auto")
    if provider == "mock":
        return [MockTTS()]
    voices: list[TTSProvider] = []
    if provider == "piper":
        piper = PiperTTS(getattr(tts_cfg, "piper_model", None), binary=getattr(tts_cfg, "piper_binary", None))
        if piper.available():
            voices.append(piper)
        else:
            log.info("assistant local voice: piper unavailable (%s)", piper.unavailable_reason())
    espeak = EspeakTTS(voice=getattr(tts_cfg, "voice", "en-us+f3"), pitch=getattr(tts_cfg, "pitch", 65))
    if espeak.available():
        voices.append(espeak)
    voices.append(MockTTS())
    return voices


class LocalTTS:
    name = "local"
    is_cloud = False

    def __init__(self, voices: list[TTSProvider], *, rate: float = 1.0, chunk_seconds: float = CHUNK_SECONDS) -> None:
        self.voices = list(voices) or [MockTTS()]
        self.rate = rate
        self.chunk_seconds = chunk_seconds
        first = self.voices[0]
        self.sample_rate = int(first.sample_rate() if isinstance(first, PiperTTS) else 22050)
        self.voice = first.name  # the one that spoke last

    @classmethod
    def from_config(cls, tts_cfg: Any) -> "LocalTTS":
        return cls(local_voices(tts_cfg))

    def missing(self) -> str | None:
        return None

    def _synthesize(self, text: str) -> tuple[np.ndarray, int, str]:
        for voice in self.voices:
            try:
                samples, sr = voice.synthesize(text, self.rate)
            except (AudioError, OSError, ValueError) as e:
                log.warning("assistant local voice %s failed (%s)", voice.name, type(e).__name__)
                continue
            if len(samples):
                return np.asarray(samples, dtype=np.float32), int(sr), voice.name
        raise SpeechError("bad_audio", "no local voice worked")

    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        samples, sr, voice = await asyncio.to_thread(self._synthesize, text)
        self.sample_rate, self.voice = sr, voice
        n = max(1, int(sr * self.chunk_seconds))
        for i in range(0, len(samples), n):
            yield samples[i:i + n]

    async def warm(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


__all__ = ["LocalTTS", "local_voices"]
