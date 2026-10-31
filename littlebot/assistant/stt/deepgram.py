"""Deepgram Flux (cloud): streaming speech-to-text that also knows when the person has finished their turn.

API (https://developers.deepgram.com/reference/speech-to-text/listen-flux and
https://developers.deepgram.com/docs/flux/quickstart, read 2026-10-30): a websocket to
``wss://api.deepgram.com/v2/listen`` with ``model=flux-general-en``, ``encoding=linear16``, ``sample_rate=16000``,
``eot_threshold`` (how sure Flux must be that the turn ended, 0.5-1.0) and ``eot_timeout_ms`` (a turn ends after this
much quiet whatever), plus ``mip_opt_out=true`` (the audio isn't used to train Deepgram's models; no price change for
pay-as-you-go accounts since 2026-03-05); header ``Authorization: Token <key>``. The audio goes up as binary
messages of 80 ms (Deepgram's strong recommendation: 2,560 bytes of 16-bit samples). Flux answers with JSON:
``Connected``; ``TurnInfo`` with ``event`` StartOfTurn / Update / EagerEndOfTurn / TurnResumed / EndOfTurn, the
``turn_index``, the ``transcript`` so far and its ``words`` (``word``, ``confidence``); ``Warning``; ``Error``.
Client messages: ``{"type": "ForceEndTurn"}`` ends the turn now, ``{"type": "CloseStream"}`` before closing.

Here: StartOfTurn -> ``start_of_turn``; Update (and the eager ones) -> ``partial`` with the words so far;
EagerEndOfTurn -> also ``eager_end_of_turn`` (the session starts the answer, says it only after EndOfTurn);
TurnResumed -> ``turn_resumed`` (that answer is dropped);
EndOfTurn -> ``end_of_turn`` with the turn's words (confidence: the words' average). ``finish()`` sends ForceEndTurn;
``reset()`` forgets the turn in progress (ForceEndTurn, and that turn's late events are ignored), so the next words
are a new turn. A failure (connection lost, key refused, Flux's Error) is one ``error`` event with its kind; the
session can then open the next provider.

The key: ``DEEPGRAM_API_KEY`` in .env, or data/deepgram_api_key (chmod 600). It goes only into the handshake
header; the websockets library logs every header at DEBUG level, so it is given a logger of its own that never
logs at DEBUG.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import numpy as np

from littlebot.assistant.speech import STT_RATE, CloudOk, OnEvent, SpeechError, STTEvent
from littlebot.assistant.speech_http import is_allowed, status_kind
from littlebot.assistant.stt.base import emit
from littlebot.audio import dsp

log = logging.getLogger("littlebot.assistant.stt.deepgram")

URL = "wss://api.deepgram.com/v2/listen"
PACKET_MS = 80
PACKET_BYTES = STT_RATE * 2 * PACKET_MS // 1000  # 2560
OPEN_TIMEOUT_S = 5.0
CLOSE_WAIT_S = 1.0  # after CloseStream, how long Flux may take to close
FORCE_END = json.dumps({"type": "ForceEndTurn"})
CLOSE_STREAM = json.dumps({"type": "CloseStream"})

# (url, headers) -> an open websocket: ``await send(bytes | str)``, ``await recv() -> str | bytes``, ``await close()``
WsConnect = Callable[[str, dict[str, str]], Awaitable[Any]]


def websockets_installed() -> bool:
    try:
        return importlib.util.find_spec("websockets") is not None
    except (ImportError, ValueError):
        return False


def ws_error(e: BaseException) -> SpeechError:
    """A websocket failure as a SpeechError (its kind and class name only)."""
    if isinstance(e, SpeechError):
        return e
    if isinstance(e, TimeoutError):
        return SpeechError("timeout", "connecting")
    status = getattr(getattr(e, "response", None), "status_code", None)  # websockets' InvalidStatus
    if isinstance(status, int):
        return SpeechError(status_kind(status), f"HTTP {status}")
    if type(e).__name__.startswith("ConnectionClosed"):
        return SpeechError("closed", type(e).__name__)
    return SpeechError("network", type(e).__name__)


async def connect_websocket(url: str, headers: dict[str, str]) -> Any:
    """The real connection (``websockets``, imported here: it is optional)."""
    from websockets.asyncio.client import connect  # noqa: PLC0415

    quiet = logging.getLogger("littlebot.assistant.stt.deepgram.ws")
    if quiet.level == logging.NOTSET or quiet.level < logging.INFO:
        quiet.setLevel(logging.INFO)  # never DEBUG: it would print the handshake's headers (the key)
    try:
        return await connect(url, additional_headers=headers, open_timeout=OPEN_TIMEOUT_S, compression=None,
                             max_size=2 ** 20, logger=quiet)
    except Exception as e:  # noqa: BLE001
        raise ws_error(e) from None


def _confidence(words: Any) -> float:
    confs = []
    for w in words or ():
        try:
            confs.append(float(w.get("confidence")))
        except (AttributeError, TypeError, ValueError):
            continue
    return round(sum(confs) / len(confs), 3) if confs else 1.0


class FluxStream:
    """One session's ``STTStream`` over an open Flux websocket."""

    def __init__(self, ws: Any, on_event: OnEvent, *, cloud_ok: CloudOk) -> None:
        self._ws = ws
        self._on_event = on_event
        self._cloud_ok = cloud_ok
        self._buf = bytearray()
        self._turn: int | None = None  # the turn in progress (Flux's turn_index), None between turns
        self._old = -1  # events of turns up to this index are ignored (ended, or forgotten by reset)
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
        if self._closed or self._failed:
            return
        if self._buf:
            packet, self._buf = bytes(self._buf), bytearray()
            await self._send(packet)
        if self._turn is not None:
            await self._send(FORCE_END)

    async def reset(self) -> None:
        self._partial = ""
        if self._turn is not None:
            self._old = max(self._old, self._turn)
            self._turn = None
            if not (self._closed or self._failed):
                await self._send(FORCE_END)  # Flux starts the next turn fresh; this one's end is ignored

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buf = bytearray()
        reader = self._reader
        if not self._failed:
            try:
                await asyncio.wait_for(self._ws.send(CLOSE_STREAM), CLOSE_WAIT_S)
                if reader is not None:
                    await asyncio.wait([reader], timeout=CLOSE_WAIT_S)
            except Exception:  # noqa: BLE001 - closing anyway
                log.debug("deepgram: CloseStream failed", exc_info=True)
        if reader is not None and not reader.done():
            reader.cancel()
        try:
            await asyncio.wait_for(self._ws.close(), CLOSE_WAIT_S)
        except Exception:  # noqa: BLE001
            log.debug("deepgram: closing the websocket failed", exc_info=True)

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
        kind = m.get("type")
        if kind == "TurnInfo":
            await self._turn_info(m)
        elif kind == "Error":
            code = str(m.get("code") or "").upper()
            log.warning("deepgram: error %s", code[:60] or "?")
            await self._fail("quota" if ("CREDIT" in code or "QUOTA" in code or "BALANCE" in code) else
                             "auth" if ("AUTH" in code or "PERMISSION" in code) else
                             "rate_limit" if ("RATE" in code or "LIMIT" in code) else "server_error")
        elif kind == "Warning":
            log.info("deepgram: warning %s", str(m.get("code") or "?")[:60])
        elif kind == "Connected":
            log.debug("deepgram: connected")

    async def _turn_info(self, m: dict[str, Any]) -> None:
        event = str(m.get("event") or "")
        try:
            index = int(m["turn_index"])
        except (KeyError, TypeError, ValueError):
            index = self._turn if self._turn is not None else self._old + 1
        if index <= self._old:
            return  # a turn that ended or was forgotten
        text = str(m.get("transcript") or "").strip()
        if self._turn != index:
            self._turn, self._partial = index, ""
            await emit(self._on_event, STTEvent("start_of_turn"))
        if event == "EndOfTurn":
            self._turn, self._old, self._partial = None, max(self._old, index), ""
            self._log_lag(m)
            # no words (a cough): an empty end, so the session stops waiting for them
            await emit(self._on_event, STTEvent("end_of_turn", text=text, confidence=_confidence(m.get("words"))))
            return
        if text and text != self._partial:  # Update, StartOfTurn with words, EagerEndOfTurn, TurnResumed
            self._partial = text
            await emit(self._on_event, STTEvent("partial", text=text))
        if event == "EagerEndOfTurn" and text:  # probably done: the answer may start (said only after EndOfTurn)
            await emit(self._on_event, STTEvent("eager_end_of_turn", text=text))
        elif event == "TurnResumed":  # they went on talking: a started answer is dropped
            await emit(self._on_event, STTEvent("turn_resumed", text=text))

    def _log_lag(self, m: dict[str, Any]) -> None:
        """How long after their last word Flux ended the turn, and how far behind the audio sent it was then (it
        works through what it is sent at about real time: a burst at the start delays every turn after it)."""
        try:
            window = float(m["audio_window_end"])
            words = m.get("words") or []
            last = float(words[-1]["end"]) if words else None
        except (KeyError, TypeError, ValueError, IndexError):
            return
        sent = self.packets * PACKET_BYTES / 2 / STT_RATE
        log.info("deepgram: end of turn %s, %.2f s behind the audio sent",
                 f"{window - last:.2f} s after their last word" if last is not None else "(no words)",
                 max(0.0, sent - window))

    async def _fail(self, kind: str) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        log.warning("assistant stt: deepgram stopped (%s)", kind)
        await emit(self._on_event, STTEvent("error", detail=kind))


class DeepgramFlux:
    """The Flux ``STTProvider`` (``streaming``: Flux finds the end of a turn itself)."""

    name = "deepgram"
    streaming = True
    is_cloud = True

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "flux-general-en",
                 eot_threshold: float = 0.7, eot_timeout_ms: int = 3000, eager_eot_threshold: float | None = None,
                 url: str = URL, ws_connect: WsConnect | None = None, open_timeout: float = OPEN_TIMEOUT_S) -> None:
        self._key = key
        self._cloud_ok = cloud_ok
        self.model = model
        self.eot_threshold = eot_threshold
        self.eot_timeout_ms = eot_timeout_ms
        self.eager_eot_threshold = eager_eot_threshold
        self.url = url
        self._ws_connect = ws_connect
        self.open_timeout = open_timeout

    def __repr__(self) -> str:  # never the key
        return f"DeepgramFlux(model={self.model!r})"

    def _key_value(self) -> str:
        try:
            return str(self._key() or "")
        except Exception:  # noqa: BLE001
            return ""

    def missing(self) -> str | None:
        if not self._key_value():
            return "no_key"
        if self._ws_connect is None and not websockets_installed():
            return "not_installed"
        if not is_allowed(self._cloud_ok):
            return "cloud_off"
        return None

    def stream_url(self) -> str:
        params: dict[str, Any] = {"model": self.model, "encoding": "linear16", "sample_rate": STT_RATE,
                                  "eot_threshold": self.eot_threshold, "eot_timeout_ms": int(self.eot_timeout_ms),
                                  "mip_opt_out": "true"}
        if self.eager_eot_threshold is not None:
            params["eager_eot_threshold"] = self.eager_eot_threshold
        return f"{self.url}?{urlencode(params)}"

    async def open(self, on_event: OnEvent) -> FluxStream:
        why = self.missing()
        if why:
            raise SpeechError(why)
        connect = self._ws_connect or connect_websocket
        headers = {"Authorization": f"Token {self._key_value()}"}
        try:
            ws = await asyncio.wait_for(connect(self.stream_url(), headers), self.open_timeout)
        except Exception as e:  # noqa: BLE001
            err = ws_error(e)
            log.warning("assistant stt: deepgram did not connect (%s)", err.kind)
            raise err from None
        stream = FluxStream(ws, on_event, cloud_ok=self._cloud_ok)
        stream.start()
        return stream


__all__ = ["DeepgramFlux", "FluxStream", "WsConnect", "connect_websocket", "websockets_installed", "ws_error"]
