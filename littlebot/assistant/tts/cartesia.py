"""Cartesia Sonic (cloud): the fastest first audio of the voices LittleBot can use (under 100 ms of model time by
Cartesia's figures; docs/ASSISTANT_SPEECH.md has the numbers and prices).

One streamed POST per sentence to ``/tts/bytes`` (API reference: https://docs.cartesia.ai/api-reference/tts/bytes,
read 2026-10-30): headers ``Cartesia-Version: 2026-08-14`` and ``Authorization: Bearer <key>``; body ``model_id``,
``transcript``, ``voice: {id}``, ``language``, ``output_format: {container: raw, encoding: pcm_s16le,
sample_rate: 48000}`` (the speaker's own rate: nothing to resample) and, for a speed other than "normal",
``generation_config.speed`` (0.6-1.5x, sonic-3 and newer). The answer is the sound itself, sent as it is made.
Cartesia's websocket API would save only the request's few milliseconds over this kept-open connection, so the
simpler HTTP stream is used.

The key: ``CARTESIA_API_KEY`` in .env, or data/cartesia_api_key (chmod 600).
"""

from __future__ import annotations

from typing import Any, Callable

from littlebot.assistant.speech import CloudOk
from littlebot.assistant.tts.base import HttpPcmTTS, Request

URL = "https://api.cartesia.ai/tts/bytes"
API_VERSION = "2026-08-14"
RATE = 48000  # one of 8000, 16000, 22050, 24000, 44100, 48000
# assistant.cartesia_speed -> generation_config.speed (the API takes 0.6-1.5)
SPEEDS = {"slowest": 0.7, "slow": 0.85, "normal": 1.0, "fast": 1.15, "fastest": 1.3}


class CartesiaTTS(HttpPcmTTS):
    name = "cartesia"
    sample_rate = RATE
    warm_url = "https://api.cartesia.ai/"

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "sonic-3",
                 voice: str = "f786b574-daa5-4673-aa0c-cbe3e8534c02", speed: str = "normal", language: str = "en",
                 url: str = URL, transport: Any = None) -> None:
        super().__init__(key=key, cloud_ok=cloud_ok, transport=transport)
        self.model = model
        self.voice = voice
        self.speed = speed
        self.language = language
        self.url = url

    def request(self, text: str, key: str) -> Request:
        body: dict[str, Any] = {
            "model_id": self.model,
            "transcript": text,
            "voice": {"id": self.voice},
            "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": RATE},
        }
        if self.language:
            body["language"] = self.language
        speed = SPEEDS.get(self.speed, 1.0)
        if speed != 1.0:
            body["generation_config"] = {"speed": speed}
        headers = {"Cartesia-Version": API_VERSION, "Authorization": f"Bearer {key}"}
        return Request(self.url, headers, body)


__all__ = ["CartesiaTTS"]
