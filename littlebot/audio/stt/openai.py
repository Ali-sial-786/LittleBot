"""OpenAI speech-to-text (cloud). Gated on ``ctx.privacy.cloud_enabled`` + API key.

The recording is encoded to WAV in memory and uploaded; nothing touches the
disk. Refusals / failures raise so the service falls back to the local STT.
"""

from __future__ import annotations

import logging
from typing import Callable

import httpx
import numpy as np

from littlebot.audio import dsp
from littlebot.audio.errors import CloudNotAllowed, STTError
from littlebot.audio.stt.base import STTProvider, STTResult

log = logging.getLogger("littlebot.audio.stt.openai")

TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
MIN_SECONDS = 0.3  # do not upload clicks / coughs


class OpenAISTT(STTProvider):
    name = "openai"
    is_cloud = True

    def __init__(self, *, api_key: Callable[[], str | None], cloud_allowed: Callable[[], bool],
                 model: str = "whisper-1", language: str = "en", timeout: float = 15.0,
                 url: str = TRANSCRIPTIONS_URL, transport: httpx.BaseTransport | None = None) -> None:
        self._api_key = api_key
        self._cloud_allowed = cloud_allowed
        self.model = model
        self.language = language
        self.timeout = timeout
        self.url = url
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

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> STTResult | None:
        if not self._cloud_allowed():
            raise CloudNotAllowed("cloud speech recognition is disabled by the privacy settings")
        key = self._api_key()
        if not key:
            raise CloudNotAllowed("OPENAI_API_KEY is not configured")
        if len(samples) < MIN_SECONDS * sample_rate:
            return None
        wav = dsp.encode_wav(samples, sample_rate)  # in memory only
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=5.0), transport=self._transport) as client:
                resp = client.post(self.url, data=data, files={"file": ("speech.wav", wav, "audio/wav")},
                                   headers={"Authorization": f"Bearer {key}"})
        except httpx.TimeoutException as e:
            raise STTError("OpenAI transcription timed out") from e
        except httpx.HTTPError as e:
            raise STTError(f"OpenAI transcription request failed: {type(e).__name__}") from e
        finally:
            del wav
        if resp.status_code == 401:
            raise STTError("OpenAI transcription rejected the API key (401)")
        if resp.status_code >= 400:
            raise STTError(f"OpenAI transcription error HTTP {resp.status_code}")
        try:
            text = str(resp.json().get("text", "")).strip()
        except ValueError as e:
            raise STTError("OpenAI transcription returned invalid JSON") from e
        return STTResult(text, 0.9) if text else None
