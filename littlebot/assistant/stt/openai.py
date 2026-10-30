"""OpenAI gpt-4o-mini-transcribe (cloud): the turn's words once LittleBot's endpointer says the turn is over.

Works with the OpenAI key the robot already has for GPT-Live. One POST per turn to ``/v1/audio/transcriptions``
(API reference: https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create,
read 2026-10-30): multipart ``file`` (the turn as a 16 kHz WAV made in memory, never written anywhere), ``model``
(``assistant.openai_stt_model``), ``language`` (``assistant.language``) and ``response_format: json``; the answer's
``text`` is the words. A turn's words come back roughly 0.3-0.8 s after the pause (an estimate, not measured on
the robot).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
import numpy as np

from littlebot.assistant.speech import STT_RATE, CloudOk, SpeechError
from littlebot.assistant.speech_http import KeptClient, http_error, is_allowed, status_kind
from littlebot.audio import dsp

log = logging.getLogger("littlebot.assistant.stt.openai")

URL = "https://api.openai.com/v1/audio/transcriptions"
MIN_SECONDS = 0.3  # shorter isn't words: not uploaded
TIMEOUT_S = 10.0


class OpenAITranscriber:
    """A ``BatchSTT`` (used through stt/endpoint.py)."""

    name = "openai"
    streaming = False
    is_cloud = True

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "gpt-4o-mini-transcribe",
                 language: str = "en", url: str = URL, timeout: float = TIMEOUT_S, transport: Any = None) -> None:
        self._key = key
        self._cloud_ok = cloud_ok
        self.model = model
        self.language = language
        self.url = url
        self.timeout = timeout
        self._http = KeptClient(transport, read_timeout=timeout)

    def __repr__(self) -> str:  # never the key
        return f"OpenAITranscriber(model={self.model!r})"

    def _key_value(self) -> str:
        try:
            return str(self._key() or "")
        except Exception:  # noqa: BLE001
            return ""

    def missing(self) -> str | None:
        if not self._key_value():
            return "no_key"
        if not is_allowed(self._cloud_ok):
            return "cloud_off"
        return None

    async def transcribe(self, samples: np.ndarray) -> str:
        why = self.missing()
        if why:
            raise SpeechError(why)
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if len(x) < MIN_SECONDS * STT_RATE:
            return ""
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        wav = dsp.encode_wav(x, STT_RATE)  # in memory only
        try:
            resp = await self._http.get().post(self.url, data=data, files={"file": ("turn.wav", wav, "audio/wav")},
                                               headers={"Authorization": f"Bearer {self._key_value()}"})
        except httpx.HTTPError as e:
            raise http_error(e) from None
        finally:
            del wav
        if resp.status_code != 200:
            raise SpeechError(status_kind(resp.status_code), f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            raise SpeechError("server_error", "not JSON") from None
        return str(body.get("text", "") if isinstance(body, dict) else "").strip()

    async def warm(self) -> None:
        """Open the connection (best effort; called when a stream opens)."""
        if self.missing() is None:
            await self._http.warm("https://api.openai.com/v1/models")

    async def aclose(self) -> None:
        await self._http.aclose()


__all__ = ["OpenAITranscriber"]
