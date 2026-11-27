"""OpenAI gpt-4o-mini-tts (cloud), streamed: works with the OpenAI key the robot already has for GPT-Live.

One streamed POST per sentence to ``/v1/audio/speech`` (API reference:
https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create and the
text-to-speech guide, read 2026-10-30): ``Authorization: Bearer <key>``; body ``model``, ``input``, ``voice``,
``response_format: "pcm"`` (raw 16-bit little-endian samples at 24 kHz, sent as they are made: the guide's
lowest-latency format) and, for the gpt-4o models, ``instructions`` (how to say it). OpenAI says the gpt-4o-mini-tts
models shut down on 2027-01-06: ``assistant.openai_tts_model`` then needs a newer model.
"""

from __future__ import annotations

from typing import Any, Callable

from littlebot.assistant.speech import CloudOk
from littlebot.assistant.tts.base import HttpPcmTTS, Request

URL = "https://api.openai.com/v1/audio/speech"
RATE = 24000
INSTRUCTIONS = ("Speak warmly and naturally, like a kind friend chatting at home: upbeat but calm, at a relaxed "
                "conversational pace, with natural pauses.")


class OpenAITTS(HttpPcmTTS):
    name = "openai"
    sample_rate = RATE
    warm_url = "https://api.openai.com/v1/models"

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "gpt-4o-mini-tts",
                 voice: str = "coral", instructions: str = INSTRUCTIONS, url: str = URL,
                 transport: Any = None) -> None:
        super().__init__(key=key, cloud_ok=cloud_ok, transport=transport)
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.url = url

    def request(self, text: str, key: str) -> Request:
        body: dict[str, Any] = {"model": self.model, "input": text, "voice": self.voice, "response_format": "pcm"}
        if self.instructions and self.model.startswith("gpt-"):  # tts-1 / tts-1-hd refuse instructions
            body["instructions"] = self.instructions
        return Request(self.url, {"Authorization": f"Bearer {key}"}, body)


__all__ = ["OpenAITTS"]
