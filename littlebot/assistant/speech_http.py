"""What the cloud speech providers share: the cloud gate, one kept-open HTTP connection per provider, and HTTP
failures as a :class:`~littlebot.assistant.speech.SpeechError` kind.

A new TLS connection costs ~0.1-0.4 s on the robot's Wi-Fi, more than a fast voice takes to start talking, so each
provider keeps one ``httpx.AsyncClient`` open (``KeptClient``; ``warm()`` opens it before the first sentence).
Errors carry a kind and the HTTP status only: never the request (the words), the response body or the key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx
import numpy as np

from littlebot.assistant.speech import CloudOk, SpeechError

log = logging.getLogger("littlebot.assistant.speech")

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0  # between two pieces of a streamed answer
KEEPALIVE_S = 120.0  # an idle connection is kept this long (a follow-up question reuses it)


def cloud_ok_for(ctx: Any) -> CloudOk:
    """``ctx``'s cloud gate: not in offline mode and the parent's "cloud" switch on (checked at every call)."""

    def cloud_ok() -> bool:
        try:
            return (not ctx.config.is_offline) and bool(ctx.privacy.cloud_enabled)
        except Exception:  # noqa: BLE001 - can't tell: no network
            return False

    return cloud_ok


def key_reader(ctx: Any, name: str) -> Callable[[], str]:
    """``() -> the key`` ("" when unset), read from ``ctx.config`` when needed and never kept elsewhere."""

    def key() -> str:
        try:
            return ctx.config.secret(name)
        except Exception:  # noqa: BLE001
            return ""

    return key


def is_allowed(cloud_ok: CloudOk) -> bool:
    try:
        return bool(cloud_ok())
    except Exception:  # noqa: BLE001 - can't tell: no network
        return False


def status_kind(status: int) -> str:
    """An HTTP error status as a SpeechError kind."""
    if status == 402:  # Cartesia / ElevenLabs: out of credits ("quota_exceeded": Cartesia on the robot, 2026-10-31)
        return "quota"
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server_error"
    return "bad_request"


def http_error(e: Exception) -> SpeechError:
    """An httpx exception as a SpeechError (its class name only: an httpx message can hold the URL)."""
    if isinstance(e, httpx.TimeoutException):
        return SpeechError("timeout", type(e).__name__)
    return SpeechError("network", type(e).__name__)


class KeptClient:
    """One ``httpx.AsyncClient``, made on first use in the running loop (and again if the loop changed: tests)."""

    def __init__(self, transport: Any = None, *, read_timeout: float = READ_TIMEOUT_S) -> None:
        self._transport = transport
        self._read_timeout = read_timeout
        self._client: httpx.AsyncClient | None = None
        self._loop: int | None = None

    def get(self) -> httpx.AsyncClient:
        loop = id(asyncio.get_running_loop())
        if self._client is None or self._client.is_closed or self._loop != loop:
            self._client = httpx.AsyncClient(
                transport=self._transport, follow_redirects=False,
                timeout=httpx.Timeout(self._read_timeout, connect=CONNECT_TIMEOUT_S),
                limits=httpx.Limits(max_keepalive_connections=2, keepalive_expiry=KEEPALIVE_S))
            self._loop = loop
        return self._client

    async def warm(self, url: str, timeout: float = 3.0) -> None:
        """Open the connection (a bare HEAD: no key, no words; whatever the answer, the connection stays open)."""
        try:
            await asyncio.wait_for(self.get().request("HEAD", url), timeout)
        except Exception:  # noqa: BLE001 - best effort: the real request connects anyway
            log.debug("warming %s failed", url.split("/")[2] if "//" in url else "?")

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                log.debug("closing an HTTP client failed", exc_info=True)


class PCM16Decoder:
    """Raw signed 16-bit little-endian mono bytes, in pieces of any length, -> float32 chunks (-1..1). An odd
    byte at the end of a piece waits for the next one."""

    def __init__(self) -> None:
        self._rest = b""

    def decode(self, data: bytes) -> np.ndarray:
        data = self._rest + data
        cut = len(data) // 2 * 2
        self._rest = data[cut:]
        if not cut:
            return np.zeros(0, dtype=np.float32)
        return np.frombuffer(data[:cut], dtype="<i2").astype(np.float32) / 32768.0


def not_audio(content_type: str) -> bool:
    """A 200 answer that is a message (JSON, HTML, text) and not sound: never played as noise."""
    ctype = content_type.split(";")[0].strip().lower()
    return ctype.startswith(("application/json", "text/"))


__all__ = ["KeptClient", "PCM16Decoder", "cloud_ok_for", "http_error", "is_allowed", "key_reader", "not_audio",
           "status_kind"]
