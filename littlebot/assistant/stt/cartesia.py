"""Cartesia Ink 2 (cloud): streaming speech-to-text that finds the end of a turn itself, like Deepgram Flux.

API (https://docs.cartesia.ai/api-reference/stt/turns/websocket and https://docs.cartesia.ai/use-the-api/stt/turns,
read 2026-11-02; checked on the robot the same day): a websocket to ``wss://api.cartesia.ai/stt/turns/websocket``
with ``model=ink-2``, ``encoding=pcm_s16le``, ``sample_rate=16000``, ``cartesia_version=2026-08-14``,
``turn_end_timeout_ms`` (a turn ends after this much quiet whatever, 640-11200) and ``keyterm`` (repeated, at
most 100 and 1,200 characters; only when it connects: they can't be changed mid-stream); header
``X-API-Key: <key>``. The audio goes up as binary messages of 100 ms (3,200 bytes of 16-bit samples). It answers
with JSON, each with ``type``, ``request_id`` and (turn events) ``turn_id``: ``connected``; ``turn.start``;
``turn.update`` with the ``transcript`` so far; ``turn.eager_end`` (probably done); ``turn.resume`` (they went
on); ``turn.end`` with the turn's words; ``error`` (``status_code``, ``error_code``, ``message``). The client
sends ``{"type": "close"}`` before closing (it flushes the last words first). It gives no word confidences.

Here, as for Flux (stt/deepgram.py): turn.start -> ``start_of_turn``; turn.update and turn.eager_end ->
``partial``; turn.eager_end -> also ``eager_end_of_turn``; turn.resume -> ``turn_resumed``; turn.end ->
``end_of_turn``. It has no "end the turn now", so ``finish()`` ends the turn here with the words so far (its own
end of that turn is then ignored), and ``reset()`` ignores the rest of the turn in progress. A failure is one
``error`` event with its kind.

The key: ``CARTESIA_API_KEY`` in .env, or data/cartesia_api_key (chmod 600), the same as Cartesia's voice. It goes
only into the handshake header (the websocket's logger never logs at DEBUG, see deepgram.connect_websocket).
Price: 3 credits a second of audio streamed, silence included (Cartesia's pricing page, 2026-11-02).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Sequence
from urllib.parse import urlencode

import numpy as np

from littlebot.assistant.keyterms import fit
from littlebot.assistant.speech import STT_RATE, CloudOk, OnEvent, SpeechError, STTEvent
from littlebot.assistant.speech_http import is_allowed, status_kind
from littlebot.assistant.stt.base import emit
from littlebot.assistant.stt.deepgram import WsConnect, connect_websocket, websockets_installed, ws_error
from littlebot.audio import dsp

log = logging.getLogger("littlebot.assistant.stt.cartesia")

URL = "wss://api.cartesia.ai/stt/turns/websocket"
API_VERSION = "2026-08-14"
PACKET_MS = 100
PACKET_BYTES = STT_RATE * 2 * PACKET_MS // 1000  # 3200
OPEN_TIMEOUT_S = 5.0
CLOSE_WAIT_S = 1.0
CLOSE = json.dumps({"type": "close"})


class InkStream:
    """One session's ``STTStream`` over an open Ink websocket."""

    def __init__(self, ws: Any, on_event: OnEvent, *, cloud_ok: CloudOk) -> None:
        self._ws = ws
        self._on_event = on_event
        self._cloud_ok = cloud_ok
        self._buf = bytearray()
        self._turn: str | None = None  # the turn in progress (its turn_id), None between turns
        self._ignored: str | None = None  # a turn ended here (finish) or forgotten (reset): its late events
        self._partial = ""
        self._closed = False
        self._failed = False
        self.packets = 0
        self._reader: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read())

    # ------------------------------------------------------------ STTStream
    async def feed(self, samples: np.ndarray) -> None:
        if self._closed or self._failed:
            return
        self._buf += dsp.to_int16(np.asarray(samples, dtype=np.float32).reshape(-1)).tobytes()
        while len(self._buf) >= PACKET_BYTES and not self._failed:
            packet = bytes(self._buf[:PACKET_BYTES])
            del self._buf[:PACKET_BYTES]
            await self._send(packet)

    async def finish(self) -> None:
        """No "end the turn now" in Ink's API: the turn ends here, with the words so far."""
        if self._closed or self._failed or self._turn is None:
            return
        text, self._ignored, self._turn, self._partial = self._partial, self._turn, None, ""
        await emit(self._on_event, STTEvent("end_of_turn", text=text, confidence=1.0))

    async def reset(self) -> None:
        self._partial = ""
        if self._turn is not None:
            self._ignored, self._turn = self._turn, None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buf = bytearray()
        reader = self._reader
        if not self._failed:
            try:
                await asyncio.wait_for(self._ws.send(CLOSE), CLOSE_WAIT_S)
                if reader is not None:
                    await asyncio.wait([reader], timeout=CLOSE_WAIT_S)
            except Exception:  # noqa: BLE001 - closing anyway
                log.debug("cartesia stt: close failed", exc_info=True)
        if reader is not None and not reader.done():
            reader.cancel()
        try:
            await asyncio.wait_for(self._ws.close(), CLOSE_WAIT_S)
        except Exception:  # noqa: BLE001
            log.debug("cartesia stt: closing the websocket failed", exc_info=True)

    # ------------------------------------------------------------ the connection
    async def _send(self, data: bytes | str) -> None:
        if not is_allowed(self._cloud_ok):  # the parent switched cloud off mid-session: nothing more goes out
            await self._fail("cloud_off")
            return
        try:
            await self._ws.send(data)
            if isinstance(data, bytes):
                self.packets += 1
        except Exception as e:  # noqa: BLE001
            await self._fail(ws_error(e).kind)

    async def _read(self) -> None:
        try:
            while True:
                msg = await self._ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    await self._handle(data)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the connection ended
            if not self._closed:
                await self._fail(ws_error(e).kind)

    async def _handle(self, m: dict[str, Any]) -> None:
        if self._closed:
            return
        kind = str(m.get("type") or "")
        if kind == "error":
            code = str(m.get("error_code") or "")[:60]
            status = m.get("status_code")
            log.warning("cartesia stt: error %s %s", status if isinstance(status, int) else "?", code or "?")
            await self._fail(status_kind(status) if isinstance(status, int) else "server_error")
            return
        if not kind.startswith("turn."):
            return
        turn = str(m.get("turn_id") or "")
        if turn and turn == self._ignored:
            return  # a turn that ended here or was forgotten
        text = str(m.get("transcript") or "").strip()
        if kind == "turn.start" or self._turn is None:
            if self._turn != turn:
                self._turn, self._partial = turn, ""
                await emit(self._on_event, STTEvent("start_of_turn"))
        if kind == "turn.end":
            self._turn, self._partial = None, ""
            await emit(self._on_event, STTEvent("end_of_turn", text=text, confidence=1.0))
            return
        if text and text != self._partial:  # turn.update, turn.eager_end
            self._partial = text
            await emit(self._on_event, STTEvent("partial", text=text))
        if kind == "turn.eager_end" and text:
            await emit(self._on_event, STTEvent("eager_end_of_turn", text=text))
        elif kind == "turn.resume":
            await emit(self._on_event, STTEvent("turn_resumed", text=self._partial))

    async def _fail(self, kind: str) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        log.warning("assistant stt: cartesia stopped (%s)", kind)
        await emit(self._on_event, STTEvent("error", detail=kind))


class CartesiaInk:
    """The Ink 2 ``STTProvider`` (``streaming``: it finds the end of a turn itself)."""

    name = "cartesia"
    streaming = True
    is_cloud = True

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "ink-2",
                 turn_end_timeout_ms: int | None = None,
                 keyterms: Sequence[str] | Callable[[], Sequence[str]] = (), url: str = URL,
                 ws_connect: WsConnect | None = None, open_timeout: float = OPEN_TIMEOUT_S) -> None:
        self._key = key
        self._cloud_ok = cloud_ok
        self.model = model
        self.turn_end_timeout_ms = turn_end_timeout_ms
        self._keyterms = keyterms
        self.url = url
        self._ws_connect = ws_connect
        self.open_timeout = open_timeout

    def __repr__(self) -> str:  # never the key
        return f"CartesiaInk(model={self.model!r})"

    def _key_value(self) -> str:
        try:
            return str(self._key() or "")
        except Exception:  # noqa: BLE001
            return ""

    @property
    def keyterms(self) -> list[str]:
        terms = self._keyterms
        if callable(terms):
            try:
                terms = terms()
            except Exception:  # noqa: BLE001 - it listens without them
                log.debug("cartesia stt: gathering the keyterms failed", exc_info=True)
                terms = ()
        return fit(terms)

    def missing(self) -> str | None:
        if not self._key_value():
            return "no_key"
        if self._ws_connect is None and not websockets_installed():
            return "not_installed"
        if not is_allowed(self._cloud_ok):
            return "cloud_off"
        return None

    def stream_url(self, keyterms: Sequence[str] | None = None) -> str:
        params: list[tuple[str, Any]] = [("model", self.model), ("encoding", "pcm_s16le"), ("sample_rate", STT_RATE),
                                         ("cartesia_version", API_VERSION)]
        if self.turn_end_timeout_ms is not None:
            params.append(("turn_end_timeout_ms", int(min(11200, max(640, self.turn_end_timeout_ms)))))
        terms = self.keyterms if keyterms is None else list(keyterms)
        params += [("keyterm", t) for t in terms]
        return f"{self.url}?{urlencode(params)}"

    async def open(self, on_event: OnEvent) -> InkStream:
        why = self.missing()
        if why:
            raise SpeechError(why)
        connect = self._ws_connect or connect_websocket
        headers = {"X-API-Key": self._key_value(), "Cartesia-Version": API_VERSION}
        terms = self.keyterms
        try:
            ws = await asyncio.wait_for(connect(self.stream_url(terms), headers), self.open_timeout)
        except Exception as e:  # noqa: BLE001
            err = ws_error(e)
            log.warning("assistant stt: cartesia did not connect (%s)", err.kind)
            raise err from None
        log.info("assistant stt: cartesia listening for %d keyterms", len(terms))
        stream = InkStream(ws, on_event, cloud_ok=self._cloud_ok)
        stream.start()
        return stream


__all__ = ["CartesiaInk", "InkStream"]
