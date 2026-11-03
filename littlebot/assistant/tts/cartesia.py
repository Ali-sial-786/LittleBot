"""Cartesia Sonic (cloud): the fastest first audio of the voices LittleBot can use (under 100 ms of model time by
Cartesia's figures; docs/ASSISTANT_SPEECH.md has the numbers and prices).

One streamed POST per sentence to ``/tts/bytes`` (API reference: https://docs.cartesia.ai/api-reference/tts/bytes,
read 2026-10-30): headers ``Cartesia-Version: 2026-08-14`` and ``Authorization: Bearer <key>``; body ``model_id``,
``transcript``, ``voice: {id}``, ``language``, ``output_format: {container: raw, encoding: pcm_s16le,
sample_rate: 48000}`` (the speaker's own rate: nothing to resample) and, for a speed other than "normal",
``generation_config.speed`` (0.6-1.5x, sonic-3 and newer). The answer is the sound itself, sent as it is made.
Cartesia's websocket API would save only the request's few milliseconds over this kept-open connection, so the
simpler HTTP stream is used.

Each request counts against the plan's limit on requests at once (the free plan: 2; more is refused with 429:
https://docs.cartesia.ai/use-the-api/concurrency-limits-and-timeouts), and a request just cut off by a barge-in
still counts for a moment. So by default LittleBot has one open at a time (``assistant.cartesia_concurrency``):
a sentence is made several times faster than it is said, so the next one is ready before it is needed.

The key: ``CARTESIA_API_KEY`` in .env, or data/cartesia_api_key (chmod 600).

``list_voices`` / ``get_voice``: Cartesia's voice library for the dashboard's voice picker (``GET /voices``, read
2026-11-02: https://docs.cartesia.ai/api-reference/voices/list; ``q`` searches names and descriptions, ``limit`` up
to 100, the next page from ``starting_after``), as plain dicts without the key.
"""

from __future__ import annotations

import re
from typing import Any, Callable

import httpx

from littlebot.assistant.speech import MOOD, CloudOk, SpeechError
from littlebot.assistant.speech_http import http_error, status_kind
from littlebot.assistant.tts.base import HttpPcmTTS, Request

URL = "https://api.cartesia.ai/tts/bytes"
VOICES_URL = "https://api.cartesia.ai/voices"
API_VERSION = "2026-08-14"
VOICE_ID = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
voices_transport: Any = None  # tests: an httpx transport for list_voices / get_voice
RATE = 48000  # one of 8000, 16000, 22050, 24000, 44100, 48000
# assistant.cartesia_speed -> generation_config.speed (the API takes 0.6-1.5); a number is used as it is
SPEEDS = {"slowest": 0.7, "slow": 0.85, "normal": 1.0, "fast": 1.15, "fastest": 1.3}
# The sentence's mood (the brain's face tag, speech.MOOD) -> generation_config.emotion
# (https://docs.cartesia.ai/build-with-cartesia/sonic-3/volume-speed-emotion: calm and content are among the
# best-supported). Only those two (and sympathetic): "happy" and "curious" made the voice suddenly loud and excited
# mid-chat (the owner, 2026-11-02), and a sentence sent with no emotion has Sonic pick one from its words.
EMOTIONS = {"happy": "content", "affectionate": "content", "playful": "content", "celebrating": "content",
            "excited": "content", "curious": "calm", "surprised": "calm", "thinking": "calm",
            "confused": "calm", "sad": "sympathetic", "sleepy": "calm", "neutral": None}
_BANG = re.compile(r"!+")  # Sonic reads "!" with a burst of energy: said as a full stop ("?!" as "?")


class CartesiaTTS(HttpPcmTTS):
    name = "cartesia"
    sample_rate = RATE
    warm_url = "https://api.cartesia.ai/"

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "sonic-3",
                 voice: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b", speed: str | float = "normal",
                 emotion: str | None = None, language: str = "en", max_concurrent: int = 1, url: str = URL,
                 transport: Any = None) -> None:
        super().__init__(key=key, cloud_ok=cloud_ok, transport=transport)
        self.max_concurrent = max_concurrent  # the chain never has more requests than this open at once
        self.model = model
        self.voice = voice
        self.speed = speed
        self.emotion = emotion  # the default emotion ("calm"): None lets the model read it from the words
        self.language = language
        self.url = url

    def request(self, text: str, key: str) -> Request:
        body: dict[str, Any] = {
            "model_id": self.model,
            "transcript": _BANG.sub(".", text).replace("?.", "?"),
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
        emotion = EMOTIONS.get(mood or "") or self.emotion  # neutral or no tag: the steady default (calm)
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


# ---------------------------------------------------------------- the voice library (the dashboard's picker)
def _voice(v: dict[str, Any]) -> dict[str, Any]:
    accents = [a for a in v.get("accents") or () if isinstance(a, dict)]
    return {"id": str(v.get("id") or ""), "name": str(v.get("name") or "")[:80],
            "tagline": str(v.get("tagline") or "")[:80], "description": str(v.get("description") or "")[:300],
            "gender": v.get("gender") or None, "accent": str(accents[0].get("accent") or "") if accents else "",
            "language": str(v.get("language") or ""), "mine": bool(v.get("is_owner"))}


async def _get(key: str, url: str, params: dict[str, Any] | None = None, timeout: float = 8.0) -> Any:
    headers = {"Cartesia-Version": API_VERSION, "Authorization": f"Bearer {key}"}
    try:
        async with httpx.AsyncClient(transport=voices_transport, timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise http_error(e) from None
    if resp.status_code != 200:
        raise SpeechError(status_kind(resp.status_code), f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise SpeechError("server_error", "not JSON") from None


async def list_voices(key: str, *, query: str = "", gender: str | None = None, language: str | None = "en",
                      mine: bool = False, after: str | None = None, limit: int = 30) -> dict[str, Any]:
    """One page of Cartesia's voices: ``{voices: [{id, name, tagline, description, gender, accent, language,
    mine}], next: <id for ``after``> or None}``. Raises SpeechError (auth, network ...)."""
    params: dict[str, Any] = {"limit": max(1, min(100, int(limit)))}
    if query.strip():
        params["q"] = query.strip()[:80]
    if gender in ("masculine", "feminine", "gender_neutral"):
        params["gender"] = gender
    if language:
        params["language"] = language
    if mine:
        params["is_owner"] = "true"
    if after and VOICE_ID.match(after):
        params["starting_after"] = after
    data = await _get(key, VOICES_URL, params)
    items = data.get("data") if isinstance(data, dict) else None
    voices = [_voice(v) for v in items or () if isinstance(v, dict) and v.get("id")]
    nxt = data.get("next_page") if isinstance(data, dict) and data.get("has_more") else None
    return {"voices": voices, "next": str(nxt) if nxt else None}


async def get_voice(key: str, voice_id: str) -> dict[str, Any]:
    """One voice by its ID (as in ``list_voices``). Raises SpeechError (``bad_request`` for an unknown ID)."""
    if not VOICE_ID.match(voice_id):
        raise SpeechError("bad_request", "not a voice ID")
    data = await _get(key, f"{VOICES_URL}/{voice_id}")
    if not isinstance(data, dict) or not data.get("id"):
        raise SpeechError("server_error", "no voice")
    return _voice(data)


__all__ = ["CartesiaTTS", "EMOTIONS", "VOICE_ID", "get_voice", "list_voices"]
