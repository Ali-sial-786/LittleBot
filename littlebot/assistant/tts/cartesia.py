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

from littlebot.assistant.speech import MOOD, CloudOk
from littlebot.assistant.tts.base import HttpPcmTTS, Request

URL = "https://api.cartesia.ai/tts/bytes"
API_VERSION = "2026-08-14"
RATE = 48000  # one of 8000, 16000, 22050, 24000, 44100, 48000
# assistant.cartesia_speed -> generation_config.speed (the API takes 0.6-1.5); a number is used as it is
SPEEDS = {"slowest": 0.7, "slow": 0.85, "normal": 1.0, "fast": 1.15, "fastest": 1.3}
# The sentence's mood (the brain's face tag, speech.MOOD) -> generation_config.emotion
# (https://docs.cartesia.ai/build-with-cartesia/sonic-3/volume-speed-emotion: calm and content are among the
# best-supported). Gentle on purpose: no "excited" or "angry", which make the voice pushy.
EMOTIONS = {"happy": "content", "affectionate": "affectionate", "playful": "content", "celebrating": "happy",
            "excited": "happy", "curious": "curious", "surprised": "curious", "thinking": "contemplative",
            "confused": "contemplative", "sad": "sympathetic", "sleepy": "calm", "neutral": None}


class CartesiaTTS(HttpPcmTTS):
    name = "cartesia"
    sample_rate = RATE
    warm_url = "https://api.cartesia.ai/"

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "sonic-3",
                 voice: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b", speed: str | float = "normal",
                 emotion: str | None = None, language: str = "en", url: str = URL, transport: Any = None) -> None:
        super().__init__(key=key, cloud_ok=cloud_ok, transport=transport)
        self.model = model
        self.voice = voice
        self.speed = speed
        self.emotion = emotion  # the default emotion ("calm"): None lets the model read it from the words
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
        config: dict[str, Any] = {}
        speed = _speed(self.speed)
        if speed != 1.0:
            config["speed"] = speed
        mood = MOOD.get()
        emotion = EMOTIONS.get(mood, self.emotion) if mood else self.emotion
        if emotion:
            config["emotion"] = emotion
        if config:
            body["generation_config"] = config
        headers = {"Cartesia-Version": API_VERSION, "Authorization": f"Bearer {key}"}
        return Request(self.url, headers, body)


def _speed(value: str | float) -> float:
    if isinstance(value, str):
        return SPEEDS.get(value, 1.0)
    try:
        return round(min(1.5, max(0.6, float(value))), 2)
    except (TypeError, ValueError):
        return 1.0


__all__ = ["CartesiaTTS", "EMOTIONS"]
