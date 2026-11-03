"""AssemblyAI Universal-3.6 Pro (cloud): streaming speech-to-text that finds the end of a turn itself, like Flux.

API (https://www.assemblyai.com/docs/api-reference/streaming-api/streaming-api, .../streaming/universal-streaming/
message-sequence, .../streaming/keyterms-prompting and .../streaming/common-session-errors-and-closures, read
2026-11-02): a websocket to ``wss://streaming.assemblyai.com/v3/ws`` with ``speech_model=universal-3-6-pro``,
``encoding=pcm_s16le``, ``sample_rate=16000``, ``max_turn_silence`` (a turn ends after this much quiet whatever,
50-10000 ms) and ``keyterms_prompt`` (a JSON list: at most 100 terms of 50 characters); header ``Authorization:
<key>`` (no "Bearer"). The audio goes up as binary messages of 50-1000 ms (100 ms here: 3,200 bytes of 16-bit
samples), at about real time (the wake word's few seconds sent at once are fine: it closes the session only when
minutes are waiting, error 3007). It answers with JSON: ``Begin`` (``id``, the ``configuration`` it uses);
``SpeechStarted``; ``Turn`` with ``turn_order`` (one number per turn, rising), ``transcript`` (the whole turn so far:
each replaces the last), ``end_of_turn``, ``turn_is_formatted`` and ``words`` (``text``, ``confidence``);
``Termination``; ``Error`` (``error_code``, ``error``) just before it closes. Client messages: ``{"type":
"ForceEndpoint"}`` ends the turn now, ``{"type": "UpdateConfiguration", "keyterms_prompt": [...]}`` replaces the
keyterms mid-stream, ``{"type": "Terminate"}`` before closing (the last words come first, then Termination).
Universal-3.6 Pro ends a turn when a short quiet (``min_turn_silence``, 128 ms in its default "balanced" mode) follows
words that read as finished, else after ``max_turn_silence`` of quiet (its own default 1280 ms; LittleBot used
Deepgram's 3000 ms at first, which made three of twenty turns end 3-4 s after Deepgram's on the robot, 2026-11-02).
Its in-between ``Turn`` messages (https://www.assemblyai.com/docs/streaming/universal-3-pro/turn-detection-and-
partials, read 2026-11-02) come shortly after speech begins (``interruption_delay`` later: 500 ms by default, 0 here,
so a barge-in is confirmed by words sooner), at each pause where the turn reads unfinished, and with
``continuous_partials=true`` also every second or few while they talk on without pausing. ``SpeechStarted`` comes
only once it has made out words (not for a cough): an early ``start_of_turn``.

Here, as for Flux (stt/deepgram.py): ``SpeechStarted`` or a turn's first ``Turn`` -> ``start_of_turn``; the next ones
-> ``partial`` with the words so far; the one with ``end_of_turn`` -> ``end_of_turn`` (confidence: the words'
average; ``unsure``: the words it was less sure of). There is no "probably done" signal, so no ``eager_end_of_turn``.
``finish()`` sends ForceEndpoint; ``reset()`` forgets the turn in progress (ForceEndpoint, and that turn's late events
are ignored). A failure is one ``error`` event with its kind.

The key: ``ASSEMBLYAI_API_KEY`` in .env, data/assemblyai_api_key (chmod 600, scripts/set_key.py assemblyai) or the
dashboard's Keys page. It goes only into the handshake header (the websocket's logger never logs at DEBUG, see
deepgram.connect_websocket). Price: about $0.45 an hour, for the whole time the connection is open (AssemblyAI's
pricing, 2026-11-02).
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
from littlebot.assistant.speech_http import is_allowed
from littlebot.assistant.stt.base import emit
from littlebot.assistant.stt.deepgram import (WsConnect, _confidence, _unsure, connect_websocket,
                                              websockets_installed, ws_error)
from littlebot.audio import dsp

log = logging.getLogger("littlebot.assistant.stt.assemblyai")

URL = "wss://streaming.assemblyai.com/v3/ws"
PACKET_MS = 100
PACKET_BYTES = STT_RATE * 2 * PACKET_MS // 1000  # 3200
OPEN_TIMEOUT_S = 5.0
CLOSE_WAIT_S = 1.0
MAX_TERM_CHARS = 50  # a longer keyterm is ignored by AssemblyAI
FORCE_END = json.dumps({"type": "ForceEndpoint"})
TERMINATE = json.dumps({"type": "Terminate"})


def error_kind(code: Any, text: str) -> str:
    """An ``Error`` message as a SpeechError kind: 1008 is the account (key, money, too many starts a minute)."""
    t = text.lower()
    if code == 1008:
        if "fund" in t or "balance" in t:
            return "quota"
        if "concurrent" in t or "too many" in t:
            return "rate_limit"
        return "auth"
    if code in (3006, 3007):
        return "bad_request"
    if code == 3008:
        return "closed"
    return "server_error"


def _words(m: dict[str, Any]) -> list[dict[str, Any]]:
    """The turn's words in Flux's shape (``word``, ``confidence``), for deepgram's helpers."""
    out = []
    for w in m.get("words") or ():
        if isinstance(w, dict):
            out.append({"word": w.get("text"), "confidence": w.get("confidence")})
    return out


class UniversalStream:
    """One session's ``STTStream`` over an open AssemblyAI websocket."""

    def __init__(self, ws: Any, on_event: OnEvent, *, cloud_ok: CloudOk, keyterms: Sequence[str] = ()) -> None:
        self._ws = ws
        self._on_event = on_event
        self._cloud_ok = cloud_ok
        self.keyterms = list(keyterms)  # what it listens for now
        self._buf = bytearray()
        self._turn: int | None = None  # the turn in progress (its turn_order), None between turns
        self._started = False  # SpeechStarted already said start_of_turn for the next turn
        self._old = -1  # events of turns up to this one are ignored (ended, or forgotten by reset)
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
        if len(self._buf) >= PACKET_BYTES // 2:  # a message under 50 ms is refused (3007)
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
                await self._send(FORCE_END)  # the next words are a new turn; this one's end is ignored

    async def set_keyterms(self, terms: Sequence[str]) -> None:
        """The keyterms from now on (UpdateConfiguration: the whole list replaces the old one), when they changed."""
        terms = [t for t in fit(terms) if len(t) <= MAX_TERM_CHARS]
        if self._closed or self._failed or not terms or terms == self.keyterms:
            return
        self.keyterms = terms
        await self._send(json.dumps({"type": "UpdateConfiguration", "keyterms_prompt": terms}))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buf = bytearray()
        reader = self._reader
        if not self._failed:
            try:
                await asyncio.wait_for(self._ws.send(TERMINATE), CLOSE_WAIT_S)
                if reader is not None:
                    await asyncio.wait([reader], timeout=CLOSE_WAIT_S)
            except Exception:  # noqa: BLE001 - closing anyway
                log.debug("assemblyai: Terminate failed", exc_info=True)
        if reader is not None and not reader.done():
            reader.cancel()
        try:
            await asyncio.wait_for(self._ws.close(), CLOSE_WAIT_S)
        except Exception:  # noqa: BLE001
            log.debug("assemblyai: closing the websocket failed", exc_info=True)

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
        if kind == "Turn":
            await self._turn_message(m)
        elif kind == "SpeechStarted":
            if self._turn is None and not self._started:
                self._started = True
                await emit(self._on_event, STTEvent("start_of_turn"))
        elif kind == "Error":
            code, text = m.get("error_code"), str(m.get("error") or "")
            log.warning("assemblyai: error %s %s", code if isinstance(code, int) else "?", text[:80] or "?")
            await self._fail(error_kind(code, text))
        elif kind == "Begin":
            model = str((m.get("configuration") or {}).get("model") or "") if isinstance(
                m.get("configuration"), dict) else ""
            log.debug("assemblyai: connected%s", f" ({model[:40]})" if model else "")

    async def _turn_message(self, m: dict[str, Any]) -> None:
        try:
            order = int(m["turn_order"])
        except (KeyError, TypeError, ValueError):
            order = self._turn if self._turn is not None else self._old + 1
        if order <= self._old:
            return  # a turn that ended or was forgotten
        text = str(m.get("transcript") or "").strip()
        if self._turn != order:
            self._turn, self._partial = order, ""
            if not self._started:
                await emit(self._on_event, STTEvent("start_of_turn"))
            self._started = False
        if m.get("end_of_turn"):
            self._turn, self._old, self._partial = None, max(self._old, order), ""
            words = _words(m)
            # no words (a cough): an empty end, so the session stops waiting for them
            await emit(self._on_event, STTEvent("end_of_turn", text=text, confidence=_confidence(words),
                                                unsure=_unsure(words)))
            return
        if text and text != self._partial:
            self._partial = text
            await emit(self._on_event, STTEvent("partial", text=text))

    async def _fail(self, kind: str) -> None:
        if self._failed or self._closed:
            return
        self._failed = True
        log.warning("assistant stt: assemblyai stopped (%s)", kind)
        await emit(self._on_event, STTEvent("error", detail=kind))


class AssemblyAIUniversal:
    """The Universal-3.6 Pro ``STTProvider`` (``streaming``: it finds the end of a turn itself)."""

    name = "assemblyai"
    streaming = True
    is_cloud = True

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "universal-3-6-pro",
                 max_turn_silence_ms: int | None = None, interruption_delay_ms: int | None = None,
                 continuous_partials: bool = False,
                 keyterms: Sequence[str] | Callable[[], Sequence[str]] = (), url: str = URL,
                 ws_connect: WsConnect | None = None, open_timeout: float = OPEN_TIMEOUT_S) -> None:
        self._key = key
        self._cloud_ok = cloud_ok
        self.model = model
        self.max_turn_silence_ms = max_turn_silence_ms  # None: AssemblyAI's own default
        self.interruption_delay_ms = interruption_delay_ms  # the first words this long after speech starts
        self.continuous_partials = continuous_partials  # words every second or so while they talk on
        self._keyterms = keyterms
        self.url = url
        self._ws_connect = ws_connect
        self.open_timeout = open_timeout

    def __repr__(self) -> str:  # never the key
        return f"AssemblyAIUniversal(model={self.model!r})"

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
                log.debug("assemblyai: gathering the keyterms failed", exc_info=True)
                terms = ()
        return [t for t in fit(terms) if len(t) <= MAX_TERM_CHARS]

    def missing(self) -> str | None:
        if not self._key_value():
            return "no_key"
        if self._ws_connect is None and not websockets_installed():
            return "not_installed"
        if not is_allowed(self._cloud_ok):
            return "cloud_off"
        return None

    def stream_url(self, keyterms: Sequence[str] | None = None) -> str:
        params: dict[str, Any] = {"speech_model": self.model, "encoding": "pcm_s16le", "sample_rate": STT_RATE}
        if self.max_turn_silence_ms is not None:
            params["max_turn_silence"] = int(min(10000, max(50, self.max_turn_silence_ms)))
        if self.interruption_delay_ms is not None:
            params["interruption_delay"] = int(max(0, self.interruption_delay_ms))
        if self.continuous_partials:
            params["continuous_partials"] = "true"
        terms = self.keyterms if keyterms is None else list(keyterms)
        if terms:
            params["keyterms_prompt"] = json.dumps(terms)
        return f"{self.url}?{urlencode(params)}"

    async def open(self, on_event: OnEvent) -> UniversalStream:
        why = self.missing()
        if why:
            raise SpeechError(why)
        connect = self._ws_connect or connect_websocket
        headers = {"Authorization": self._key_value()}
        terms = self.keyterms
        try:
            ws = await asyncio.wait_for(connect(self.stream_url(terms), headers), self.open_timeout)
        except Exception as e:  # noqa: BLE001
            err = ws_error(e)
            log.warning("assistant stt: assemblyai did not connect (%s)", err.kind)
            raise err from None
        log.info("assistant stt: assemblyai listening for %d keyterms", len(terms))
        stream = UniversalStream(ws, on_event, cloud_ok=self._cloud_ok, keyterms=terms)
        stream.start()
        return stream


__all__ = ["AssemblyAIUniversal", "UniversalStream", "error_kind"]
