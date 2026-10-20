"""OpenAI text-to-speech (cloud). Only used when a parent enabled cloud features
(``ctx.privacy.cloud_enabled``) AND an ``OPENAI_API_KEY`` is configured.

Every refusal / failure raises (``CloudNotAllowed`` / ``TTSError``) so the
AudioService falls back to the local voice. The API key is never logged.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
import numpy as np

from littlebot.audio import dsp
from littlebot.audio.errors import CloudNotAllowed, TTSError
from littlebot.audio.tts.base import TTSProvider

log = logging.getLogger("littlebot.audio.tts.openai")

SPEECH_URL = "https://api.openai.com/v1/audio/speech"
DEFAULT_INSTRUCTIONS = ("Speak slowly, warmly and softly, with a gentle, cheerful tone, "
                        "like a kind friend talking to a baby.")


class OpenAITTS(TTSProvider):
    name = "openai"
    is_cloud = True

    def __init__(self, *, api_key: Callable[[], str | None], cloud_allowed: Callable[[], bool],
                 model: str = "gpt-4o-mini-tts", voice: str = "shimmer", timeout: float = 15.0,
                 url: str = SPEECH_URL, instructions: str = DEFAULT_INSTRUCTIONS,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._api_key = api_key
        self._cloud_allowed = cloud_allowed
        self.model = model
        self.voice = voice
        self.timeout = timeout
        self.url = url
        self.instructions = instructions
        self._transport = transport

    def available(self) -> bool:
        try:
            return bool(self._cloud_allowed()) and bool(self._api_key())
        except Exception:  # noqa: BLE001
            return False

    def unavailable_reason(self) -> str:
        if not self._cloud_allowed():
            return "cloud features are turned off (privacy)"
        if not self._api_key():
            return "OPENAI_API_KEY is not set"
        return ""

    def synthesize(self, text: str, rate: float = 1.0) -> tuple[np.ndarray, int]:
        if not self._cloud_allowed():
            raise CloudNotAllowed("cloud text-to-speech is disabled by the privacy settings")
        key = self._api_key()
        if not key:
            raise CloudNotAllowed("OPENAI_API_KEY is not configured")
        payload: dict[str, Any] = {"model": self.model, "voice": self.voice, "input": text,
                                   "response_format": "wav", "speed": max(0.25, min(4.0, float(rate)))}
        if self.model.startswith("gpt-4o") and self.instructions:
            payload["instructions"] = self.instructions
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=5.0), transport=self._transport) as client:
                resp = client.post(self.url, json=payload, headers={"Authorization": f"Bearer {key}"})
        except httpx.TimeoutException as e:
            raise TTSError("OpenAI TTS timed out") from e
        except httpx.HTTPError as e:
            raise TTSError(f"OpenAI TTS request failed: {type(e).__name__}") from e
        if resp.status_code == 401:
            raise TTSError("OpenAI TTS rejected the API key (401)")
        if resp.status_code == 429:
            raise TTSError("OpenAI TTS rate limit or quota exceeded (429)")
        if resp.status_code >= 400:
            raise TTSError(f"OpenAI TTS error HTTP {resp.status_code}")
        body = resp.content
        ctype = resp.headers.get("content-type", "").lower()
        try:
            if body[:4] == b"RIFF":
                samples, sr = dsp.decode_wav(body)
            elif ctype.startswith(("audio/pcm", "audio/l16", "application/octet-stream")) and len(body) >= 480:
                # raw 24 kHz 16-bit mono PCM (response_format "pcm")
                samples, sr = dsp.as_mono_float(np.frombuffer(body[: len(body) // 2 * 2], dtype="<i2")), 24000
            else:  # never play an error page / JSON as noise
                raise TTSError(f"OpenAI TTS returned unexpected content ({ctype or 'unknown type'})")
        except ValueError as e:
            raise TTSError(f"OpenAI TTS returned unreadable audio: {e}") from e
        if len(samples) == 0:
            raise TTSError("OpenAI TTS returned no audio")
        return dsp.prepare_speech(samples, sr), sr
