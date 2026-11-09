"""Cartesia over its websocket (littlebot/assistant/tts/cartesia.py): one kept connection, a context per sentence,
a cut-off sentence cancelled, a closed connection opened again, and the HTTP stream when the websocket fails. No
network: a fake connection and httpx.MockTransport."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import httpx
import numpy as np
import pytest

import littlebot.assistant.tts.cartesia as cartesia_mod
from littlebot.assistant.speech import SpeechError
from littlebot.assistant.tts import build_tts_chain
from littlebot.assistant.tts.cartesia import API_VERSION, CartesiaTTS
from littlebot.testing import make_test_context

KEY = "sk_car_TESTKEY_websocket_0001"
CLOSE = object()


def _sine(seconds: float = 0.2, rate: int = 48000) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


def _pcm(x: np.ndarray) -> bytes:
    return np.round(np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


class ConnectionClosedError(Exception):
    """Named like websockets' own: the voice reads the kind from the name."""


class FakeWS:
    """A Cartesia websocket: answers each sentence with its sound in pieces (an odd byte split across two), a stray
    piece for another context first, then ``done`` (unless ``finish`` is off)."""

    def __init__(self, *, answer: bool = True, pieces: int = 4, finish: bool = True, error: int | None = None,
                 pong: bool = True) -> None:
        self.answer, self.pieces, self.finish, self.error, self.pong = answer, pieces, finish, error, pong
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.send_fails = False
        self.closed = False

    async def send(self, raw: str) -> None:
        if self.send_fails:
            raise ConnectionClosedError("gone")
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("cancel") or not self.answer:
            return
        cid = msg["context_id"]
        if self.error is not None:
            self.put({"type": "error", "context_id": cid, "status_code": self.error, "done": True})
            return
        data = _pcm(_sine())
        self.put({"type": "chunk", "context_id": "someone-else", "data": base64.b64encode(data[:999]).decode()})
        cut = [len(data) * i // self.pieces | 1 for i in range(1, self.pieces)]
        for a, b in zip([0, *cut], [*cut, len(data)]):
            self.put({"type": "chunk", "context_id": cid, "data": base64.b64encode(data[a:b]).decode(),
                      "done": False})
        if self.finish:
            self.put({"type": "done", "context_id": cid, "done": True})

    def put(self, msg: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(msg))

    def __aiter__(self) -> "FakeWS":
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is CLOSE:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(CLOSE)

    async def ping(self) -> asyncio.Future[float]:
        pong = asyncio.get_running_loop().create_future()
        if self.pong:
            pong.set_result(0.001)
        return pong


class Connector:
    def __init__(self, *sockets: FakeWS, fail: BaseException | None = None) -> None:
        self.sockets = list(sockets)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail = fail

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWS:
        self.calls.append((url, headers))
        if self.fail is not None:
            raise self.fail
        return self.sockets.pop(0)


class Http:
    def __init__(self) -> None:
        self.posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405)
        self.posts += 1
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, content=_pcm(_sine()))


def _voice(connect: Connector, now: list[float] | None = None) -> tuple[CartesiaTTS, Http]:
    http = Http()
    clock = (lambda: now[0]) if now is not None else (lambda: 0.0)
    voice = CartesiaTTS(key=lambda: KEY, cloud_ok=lambda: True, voice="voice-123", emotion="calm",
                        websocket=True, connect=connect, transport=httpx.MockTransport(http), clock=clock)
    return voice, http


async def _say(voice: CartesiaTTS, text: str) -> np.ndarray:
    chunks = [c async for c in voice.stream(text)]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_each_sentence_goes_over_one_kept_connection_with_its_own_context(caplog):
    ws = FakeWS()
    connect = Connector(ws)
    voice, http = _voice(connect)
    out = await _say(voice, "Hi there!")
    expected = np.frombuffer(_pcm(_sine()), dtype="<i2").astype(np.float32) / 32768.0
    assert np.array_equal(out, expected)  # the stray piece for another context isn't played
    await _say(voice, "Second one.")
    assert len(connect.calls) == 1 and http.posts == 0
    url, headers = connect.calls[0]
    assert url == f"wss://api.cartesia.ai/tts/websocket?cartesia_version={API_VERSION}"
    assert headers == {"Authorization": f"Bearer {KEY}"}
    first, second = ws.sent
    assert first["voice"] == {"mode": "id", "id": "voice-123"} and first["transcript"] == "Hi there."
    assert first["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 48000}
    assert first["generation_config"] == {"emotion": "calm"} and first["language"] == "en"
    assert first["context_id"] != second["context_id"]
    assert not any(m.get("cancel") for m in ws.sent)  # finished sentences aren't cancelled
    await voice.aclose()
    assert ws.closed and KEY not in caplog.text and KEY not in repr(voice)


async def test_a_cut_off_sentence_is_cancelled_on_its_context():
    ws = FakeWS(pieces=8, finish=False)
    voice, _ = _voice(Connector(ws))
    sound = voice.stream("A long sentence the listener talks over.")
    assert len(await sound.__anext__())
    await sound.aclose()  # a barge-in
    assert voice._waiting == {}  # at once, not when the stream is garbage collected
    await _settle()
    cid = ws.sent[0]["context_id"]
    assert ws.sent[-1] == {"context_id": cid, "cancel": True}
    assert voice._waiting == {}
    await voice.aclose()


async def test_a_connection_cartesia_closed_is_opened_again_without_http():
    old, new = FakeWS(), FakeWS()
    connect = Connector(old, new)
    voice, http = _voice(connect)
    await _say(voice, "One.")
    old.send_fails = True  # closed on Cartesia's side before the reader saw it
    assert len(await _say(voice, "Two.")) > 0
    assert len(connect.calls) == 2 and http.posts == 0 and new.sent[0]["transcript"] == "Two."
    await old.close()  # the reader of the old connection ending doesn't touch the new one
    await _settle()
    await _say(voice, "Three.")
    assert len(connect.calls) == 2 and len(new.sent) == 2
    await voice.aclose()


async def test_a_failed_websocket_gives_way_to_http_then_is_tried_again(caplog):
    now = [100.0]
    connect = Connector(FakeWS(), fail=SpeechError("network", "OSError"))
    voice, http = _voice(connect, now)
    assert len(await _say(voice, "Said over HTTP.")) > 0
    assert http.posts == 1 and "websocket failed (network); the HTTP stream for 5 minutes" in caplog.text
    await _say(voice, "Again.")
    assert http.posts == 2 and len(connect.calls) == 1  # resting: not asked
    now[0] += cartesia_mod.WS_REST_S + 1
    connect.fail = None
    await _say(voice, "Back on the websocket.")
    assert http.posts == 2 and len(connect.calls) == 2
    await voice.aclose()


async def test_no_answer_in_time_says_it_over_http(monkeypatch):
    monkeypatch.setattr(cartesia_mod, "WS_FIRST_S", 0.05)
    ws = FakeWS(answer=False)
    voice, http = _voice(Connector(ws))
    assert len(await _say(voice, "Hello?")) > 0
    assert http.posts == 1
    await _settle()
    assert ws.sent[-1] == {"context_id": ws.sent[0]["context_id"], "cancel": True} or ws.closed
    await voice.aclose()


async def test_busy_goes_to_the_chain_and_a_server_error_to_http():
    busy = FakeWS(error=429)
    voice, http = _voice(Connector(busy))
    with pytest.raises(SpeechError) as e:
        await _say(voice, "Busy.")
    assert e.value.kind == "rate_limit" and http.posts == 0  # the chain asks again: not the websocket's fault
    busy.error = 500
    assert len(await _say(voice, "Broken.")) > 0
    assert http.posts == 1
    await voice.aclose()


async def test_warm_opens_the_websocket_and_replaces_one_that_stopped_answering():
    first, second = FakeWS(pong=False), FakeWS()
    connect = Connector(first, second)
    voice, http = _voice(connect)
    await voice.warm()
    assert len(connect.calls) == 1 and http.posts == 0
    await voice.warm()  # no pong: opened again
    assert len(connect.calls) == 2
    await _settle()
    assert first.closed
    await _say(voice, "Ready.")
    assert second.sent and not first.sent
    await voice.aclose()


async def test_no_key_never_connects():
    connect = Connector(FakeWS())
    voice = CartesiaTTS(key=lambda: "", cloud_ok=lambda: True, websocket=True, connect=connect)
    with pytest.raises(SpeechError) as e:
        await _say(voice, "Hi.")
    assert e.value.kind == "no_key" and connect.calls == []
    await voice.warm()
    assert connect.calls == []


def test_the_robot_uses_the_websocket_unless_switched_off():
    def cartesia(chain):
        return next(p for p in chain.providers if p.name == "cartesia")

    assert cartesia(build_tts_chain(make_test_context())).websocket is True
    assert cartesia(build_tts_chain(make_test_context(assistant={"cartesia_websocket": False}))).websocket is False
    # tests that hand in an HTTP transport get the HTTP stream
    assert cartesia(build_tts_chain(make_test_context(), transport=httpx.MockTransport(Http()))).websocket is False
