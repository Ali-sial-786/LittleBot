"""The cloud voices' common part: one streamed HTTP POST per sentence whose answer is raw 16-bit PCM.

A subclass says where and what to send (``request``); this class sends it over the provider's kept-open
connection, hands each piece of sound on as it arrives (float32 at the provider's ``sample_rate``; the chain
resamples it to 48 kHz) and turns every failure into a :class:`SpeechError` with a short kind and the HTTP status:
never the words, the service's answer or the key.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Callable

import httpx
import numpy as np

from littlebot.assistant.speech import CloudOk, SpeechError
from littlebot.assistant.speech_http import KeptClient, PCM16Decoder, http_error, is_allowed, not_audio, status_kind

log = logging.getLogger("littlebot.assistant.tts")


class Request:
    """One sentence's HTTP request (``headers`` hold the key: never logged, never in ``repr``)."""

    __slots__ = ("url", "headers", "body", "params")

    def __init__(self, url: str, headers: dict[str, str], body: dict[str, Any],
                 params: dict[str, str] | None = None) -> None:
        self.url, self.headers, self.body, self.params = url, headers, body, params or {}

    def __repr__(self) -> str:
        return f"Request({self.url!r})"


class HttpPcmTTS:
    """A cloud voice answering with raw s16le mono PCM at ``sample_rate``."""

    name = "cloud"
    is_cloud = True
    sample_rate = 24000
    warm_url = ""  # opened by warm()

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, transport: Any = None) -> None:
        self._key = key
        self._cloud_ok = cloud_ok
        self._http = KeptClient(transport)

    def __repr__(self) -> str:  # never the key
        return f"{type(self).__name__}()"

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

    def request(self, text: str, key: str) -> Request:
        raise NotImplementedError

    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        why = self.missing()
        if why:
            raise SpeechError(why)
        req = self.request(text, self._key_value())
        decoder = PCM16Decoder()
        try:
            async with self._http.get().stream("POST", req.url, json=req.body, headers=req.headers,
                                               params=req.params or None) as resp:
                if resp.status_code != 200:
                    raise SpeechError(status_kind(resp.status_code), f"HTTP {resp.status_code}")
                if not_audio(resp.headers.get("content-type", "")):
                    raise SpeechError("bad_audio", "not sound")
                async for data in resp.aiter_bytes():
                    chunk = decoder.decode(data)
                    if len(chunk):
                        yield chunk
        except httpx.HTTPError as e:
            raise http_error(e) from None

    async def warm(self) -> None:
        if self.warm_url and self.missing() is None:
            await self._http.warm(self.warm_url)

    async def aclose(self) -> None:
        await self._http.aclose()


__all__ = ["HttpPcmTTS", "Request"]
