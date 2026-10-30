"""ElevenLabs Flash v2.5 (cloud): very natural voices, first audio ~75 ms of model time (ElevenLabs' figure).

One streamed POST per sentence to ``/v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000`` (API reference:
https://elevenlabs.io/docs/api-reference/text-to-speech/stream, read 2026-10-30): header ``xi-api-key``; body
``text``, ``model_id`` and, for the v2.5 models (``language_code`` isn't taken by multilingual v2), the language.
The answer is raw 16-bit PCM at 24 kHz as it is made (44.1 kHz needs the Pro plan; the chain resamples to 48 kHz).

The key: ``ELEVENLABS_API_KEY`` in .env, or data/elevenlabs_api_key (chmod 600).
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from littlebot.assistant.speech import CloudOk
from littlebot.assistant.tts.base import HttpPcmTTS, Request

BASE_URL = "https://api.elevenlabs.io"
RATE = 24000
FORMAT = "pcm_24000"


class ElevenLabsTTS(HttpPcmTTS):
    name = "elevenlabs"
    sample_rate = RATE
    warm_url = BASE_URL + "/"

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "eleven_flash_v2_5",
                 voice: str = "cgSgspJ2msm6clMCkdW9", language: str = "en", base_url: str = BASE_URL,
                 transport: Any = None) -> None:
        super().__init__(key=key, cloud_ok=cloud_ok, transport=transport)
        self.model = model
        self.voice = voice
        self.language = language
        self.base_url = base_url.rstrip("/")

    def request(self, text: str, key: str) -> Request:
        body: dict[str, Any] = {"text": text, "model_id": self.model}
        if self.language and "_v2_5" in self.model:
            body["language_code"] = self.language
        url = f"{self.base_url}/v1/text-to-speech/{quote(self.voice, safe='')}/stream"
        return Request(url, {"xi-api-key": key}, body, params={"output_format": FORMAT})


__all__ = ["ElevenLabsTTS"]
