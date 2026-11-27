"""Cartesia Sonic (cloud): the fastest first audio of the voices LittleBot can use (under 100 ms of model time by
Cartesia's figures; docs/ASSISTANT_SPEECH.md has the numbers and prices).

One streamed POST per sentence to ``/tts/bytes`` (API reference: https://docs.cartesia.ai/api-reference/tts/bytes,
read 2026-10-30): headers ``Cartesia-Version: 2026-08-14`` and ``Authorization: Bearer <key>``; body ``model_id``,
``transcript``, ``voice: {id}``, ``language``, ``output_format: {container: raw, encoding: pcm_s16le,
sample_rate: 48000}`` (the speaker's own rate: nothing to resample) and, for a speed other than "normal",
``generation_config.speed`` (0.6-1.5x, sonic-3 and newer). The answer is the sound itself, sent as it is made.

``websocket`` (``assistant.cartesia_websocket``, on): the same request over Cartesia's websocket instead
(https://docs.cartesia.ai/api-reference/tts/websocket: ``wss://api.cartesia.ai/tts/websocket``, the key in the
handshake's ``Authorization`` header, the API version in the URL; each sentence a JSON message with its own
``context_id`` and ``voice: {mode: id, id}``; the answers ``chunk`` (base64 PCM), ``done`` or ``error`` with that
``context_id``; ``{context_id, cancel: true}`` stops one). One connection is kept open (opened by ``warm()``, again
after Cartesia closes an idle one). On the robot (2026-11-03) its first bytes came in 145-160 ms every time and the
first loud sound at about 200 ms; over HTTP 155-305 ms and about 335 ms. A kept connection Cartesia had closed is
opened again once; a websocket that fails, or answers nothing within WS_FIRST_S, gives way to the HTTP stream for
WS_REST_S (a refused key or no credits then fail there as before; busy goes to the chain, which asks again).

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

import asyncio
import base64
import contextlib
import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
import numpy as np

from littlebot.assistant.speech import MOOD, CloudOk, SpeechError
from littlebot.assistant.speech_http import READ_TIMEOUT_S, PCM16Decoder, http_error, status_kind
from littlebot.assistant.tts.base import HttpPcmTTS, Request

log = logging.getLogger("littlebot.assistant.tts")

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
WS_URL = f"wss://api.cartesia.ai/tts/websocket?cartesia_version={API_VERSION}"
WS_OPEN_TIMEOUT_S = 1.5
WS_FIRST_S = 1.0  # its first answer comes in ~150 ms: nothing for this long and the HTTP stream says the sentence
WS_PING_S = 1.5  # warm(): a kept connection that doesn't answer a ping in this long is opened again
WS_REST_S = 300.0  # after the websocket failed: the HTTP stream this long
WS_RAISE = frozenset({"no_key", "cloud_off", "rate_limit"})  # not the websocket's fault: to the chain as they are
Connect = Callable[[str, dict[str, str]], Awaitable[Any]]


async def _connect(url: str, headers: dict[str, str]) -> Any:
    from littlebot.assistant.stt.deepgram import connect_websocket  # noqa: PLC0415 - websockets is optional

    return await connect_websocket(url, headers)


def _ws_error(e: BaseException) -> SpeechError:
    from littlebot.assistant.stt.deepgram import ws_error  # noqa: PLC0415

    return ws_error(e)


class CartesiaTTS(HttpPcmTTS):
    name = "cartesia"
    sample_rate = RATE
    warm_url = "https://api.cartesia.ai/"

    def __init__(self, *, key: Callable[[], str], cloud_ok: CloudOk, model: str = "sonic-3",
                 voice: str = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b", speed: str | float = "normal",
                 emotion: str | None = None, language: str = "en", max_concurrent: int = 1, url: str = URL,
                 transport: Any = None, websocket: bool = False, connect: Connect | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(key=key, cloud_ok=cloud_ok, transport=transport)
        self.max_concurrent = max_concurrent  # the chain never has more requests than this open at once
        self.model = model
        self.voice = voice
        self.speed = speed
        self.emotion = emotion  # the default emotion ("calm"): None lets the model read it from the words
        self.language = language
        self.url = url
        self.websocket = websocket
        self._connect = connect or _connect  # tests: a fake connection
        self._clock = clock
        self._ws: Any = None
        self._ws_loop: int | None = None
        self._ws_lock: tuple[int, asyncio.Lock] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._waiting: dict[str, tuple[Any, asyncio.Queue[Any]]] = {}  # context_id -> (its connection, answers)
        self._ws_rest_until = 0.0
        self._bg: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------ speaking
    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        if self.websocket and self._clock() >= self._ws_rest_until:
            started = False
            try:
                async with contextlib.aclosing(self._ws_stream(text)) as sound:  # closed at once on a barge-in
                    async for chunk in sound:
                        started = True
                        yield chunk
                return
            except SpeechError as e:
                if started or e.kind in WS_RAISE:
                    raise
                self._ws_failed(e)
        async with contextlib.aclosing(super().stream(text)) as sound:
            async for chunk in sound:
                yield chunk

    async def _ws_stream(self, text: str) -> AsyncIterator[np.ndarray]:
        why = self.missing()
        if why:
            raise SpeechError(why)
        body = dict(self.request(text, "").body)
        body["voice"] = {"mode": "id", "id": self.voice}
        for attempt in (1, 2):
            ws, fresh = await self._open()
            cid = uuid.uuid4().hex
            answers: asyncio.Queue[Any] = asyncio.Queue()
            self._waiting[cid] = (ws, answers)
            heard = finished = False
            try:
                try:
                    await ws.send(json.dumps({**body, "context_id": cid}))
                except Exception as e:  # noqa: BLE001
                    raise _ws_error(e) from None
                decoder = PCM16Decoder()
                while True:
                    try:
                        async with asyncio.timeout(READ_TIMEOUT_S if heard else WS_FIRST_S):
                            msg = await answers.get()
                    except TimeoutError:
                        raise SpeechError("timeout", "no answer on the websocket") from None
                    if msg is None:
                        raise SpeechError("closed", "websocket closed")
                    heard = True
                    kind = msg.get("type")
                    if kind == "chunk":
                        chunk = decoder.decode(_b64(msg.get("data")))
                        if len(chunk):
                            yield chunk
                    elif kind == "done":
                        finished = True
                        return
                    elif kind == "error":
                        finished = True
                        status = msg.get("status_code")
                        raise SpeechError(status_kind(status) if isinstance(status, int) and status >= 400
                                          else "server_error", f"websocket error {status}")
            except SpeechError as e:
                if attempt == 1 and not fresh and not heard and e.kind in ("closed", "network"):
                    self._drop_ws(ws)  # a kept connection Cartesia had closed: once more on a new one
                    continue
                raise
            finally:
                self._waiting.pop(cid, None)
                if not finished:
                    self._cancel(ws, cid)  # cut off (a barge-in) or failed: Cartesia stops making it

    # ------------------------------------------------------------ the kept-open websocket
    async def _open(self) -> tuple[Any, bool]:
        """The kept connection, opened if there is none: (connection, whether it was opened just now)."""
        loop = id(asyncio.get_running_loop())
        if self._ws is not None and self._ws_loop == loop:
            return self._ws, False
        if self._ws_lock is None or self._ws_lock[0] != loop:
            self._ws_lock = (loop, asyncio.Lock())
        async with self._ws_lock[1]:
            if self._ws is not None and self._ws_loop == loop:
                return self._ws, False
            try:
                ws = await asyncio.wait_for(self._connect(WS_URL, {"Authorization": f"Bearer {self._key_value()}"}),
                                            WS_OPEN_TIMEOUT_S)
            except SpeechError:
                raise
            except TimeoutError:
                raise SpeechError("timeout", "connecting") from None
            except Exception as e:  # noqa: BLE001
                raise _ws_error(e) from None
            self._ws, self._ws_loop = ws, loop
            self._reader = asyncio.get_running_loop().create_task(self._read(ws))
            log.debug("assistant tts: cartesia's websocket opened")
            return ws, True

    async def _read(self, ws: Any) -> None:
        """Hands each answer to the sentence it belongs to; when the connection ends, tells those still waiting."""
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                cid = str(msg.get("context_id") or "")
                if cid in self._waiting:
                    self._waiting[cid][1].put_nowait(msg)
                elif not cid and msg.get("type") == "error":  # about the connection, not one sentence
                    for w, answers in list(self._waiting.values()):
                        if w is ws:
                            answers.put_nowait(msg)
        except Exception as e:  # noqa: BLE001 - closed: those waiting are told below
            log.debug("assistant tts: cartesia's websocket ended (%s)", type(e).__name__)
        finally:
            if self._ws is ws:
                self._ws = None
            for w, answers in list(self._waiting.values()):
                if w is ws:
                    answers.put_nowait(None)

    def _ws_failed(self, e: SpeechError) -> None:
        self._ws_rest_until = self._clock() + WS_REST_S
        log.warning("assistant tts: cartesia's websocket failed (%s); the HTTP stream for %d minutes", e.kind,
                    WS_REST_S // 60)
        self._drop_ws()

    def _drop_ws(self, ws: Any = None) -> None:
        ws = ws or self._ws
        if ws is None:
            return
        if self._ws is ws:
            self._ws = None
        self._later(ws.close())

    def _cancel(self, ws: Any, cid: str) -> None:
        if self._ws is ws:
            self._later(ws.send(json.dumps({"context_id": cid, "cancel": True})))

    def _later(self, coro: Awaitable[Any]) -> None:
        """``coro`` in the background (a second at most, failures ignored): a barge-in never waits for it."""
        async def run() -> None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(coro, 1.0)

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:  # no loop (closing): nothing to send on
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------ lifecycle
    async def warm(self) -> None:
        """The websocket opened (a kept one checked with a ping) before the first sentence; else the HTTP one."""
        if not self.websocket or self._clock() < self._ws_rest_until or self.missing() is not None:
            await super().warm()
            return
        try:
            ws, fresh = await self._open()
            if not fresh and not await _alive(ws):
                self._drop_ws(ws)
                await self._open()
        except SpeechError as e:
            if e.kind in WS_RAISE:
                return
            self._ws_failed(e)
            await super().warm()

    async def aclose(self) -> None:
        ws, self._ws = self._ws, None
        same_loop = self._ws_loop == id(asyncio.get_running_loop())
        if ws is not None and same_loop:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.close(), 2.0)
        reader, self._reader = self._reader, None
        if reader is not None and same_loop and not reader.done():
            reader.cancel()
            await asyncio.wait([reader], timeout=1.0)
        for task in list(self._bg):
            task.cancel()
        await super().aclose()

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


def _b64(data: Any) -> bytes:
    try:
        return base64.b64decode(data or b"", validate=True)
    except (TypeError, ValueError):
        raise SpeechError("bad_audio", "not sound") from None


async def _alive(ws: Any) -> bool:
    ping = getattr(ws, "ping", None)
    if ping is None:
        return True
    try:
        pong = await ping()
        await asyncio.wait_for(pong, WS_PING_S)
        return True
    except Exception:  # noqa: BLE001
        return False


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
