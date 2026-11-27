"""Cartesia Ink 2 speech-to-text (littlebot/assistant/stt/cartesia.py), with a fake websocket."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np

from littlebot.assistant.speech import SpeechError, STTEvent
from littlebot.assistant.stt.cartesia import PACKET_BYTES, CartesiaInk

KEY = "test-cartesia-key-never-shown"


class Closed(Exception):
    pass


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[bytes | str] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    def push(self, kind: str, turn: str = "1", **fields: Any) -> None:
        self.inbox.put_nowait(json.dumps({"type": kind, "request_id": "r1", "turn_id": turn, **fields}))

    async def send(self, data: bytes | str) -> None:
        if self.closed:
            raise Closed("closed")
        self.sent.append(data)
        if isinstance(data, str) and json.loads(data).get("type") == "close":
            self.inbox.put_nowait(None)

    async def recv(self) -> str:
        msg = await self.inbox.get()
        if msg is None:
            raise Closed("connection lost")
        return msg

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(None)


class Connector:
    def __init__(self, error: BaseException | None = None) -> None:
        self.ws = FakeWS()
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> FakeWS:
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.ws


class Events(list):
    def __call__(self, event: STTEvent) -> None:
        self.append(event)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _ink(conn: Connector, cloud: list[bool] | None = None, **kw: Any) -> CartesiaInk:
    flag = cloud if cloud is not None else [True]
    return CartesiaInk(key=lambda: KEY, cloud_ok=lambda: flag[0], ws_connect=conn, **kw)


async def test_url_header_keyterms_and_100ms_packets():
    conn = Connector()
    stream = await _ink(conn, turn_end_timeout_ms=3000, keyterms=lambda: ["LittleBot", "Amara", "amara"]).open(Events())
    (url, headers), = conn.calls
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "wss://api.cartesia.ai/stt/turns/websocket"
    q = parse_qs(parts.query)
    assert {k: v[0] for k, v in q.items() if k != "keyterm"} == {
        "model": "ink-2", "encoding": "pcm_s16le", "sample_rate": "16000", "cartesia_version": "2026-08-14",
        "turn_end_timeout_ms": "3000"}
    assert q["keyterm"] == ["LittleBot", "Amara"]
    assert headers == {"X-API-Key": KEY, "Cartesia-Version": "2026-08-14"}
    assert KEY not in repr(_ink(conn))
    audio = np.full(4000, 0.25, dtype=np.float32)  # 250 ms
    for i in range(0, len(audio), 320):
        await stream.feed(audio[i:i + 320])
    packets = [m for m in conn.ws.sent if isinstance(m, bytes)]
    assert len(packets) == 2 and all(len(p) == PACKET_BYTES == 3200 for p in packets)
    await stream.aclose()
    assert json.loads(conn.ws.sent[-1]) == {"type": "close"} and conn.ws.closed


async def test_turn_events_as_flux_gives_them():
    conn, events = Connector(), Events()
    stream = await _ink(conn).open(events)
    ws = conn.ws
    ws.inbox.put_nowait(json.dumps({"type": "connected", "request_id": "r1"}))
    ws.push("turn.start")
    ws.push("turn.update", transcript="Hey")
    ws.push("turn.update", transcript="Hey")  # nothing new: no event
    ws.push("turn.eager_end", transcript="Hey, what's the weather?")
    ws.push("turn.resume")
    ws.push("turn.end", transcript="Hey, what's the weather tomorrow?")
    await settle()
    assert events == [STTEvent("start_of_turn"), STTEvent("partial", text="Hey"),
                      STTEvent("partial", text="Hey, what's the weather?"),
                      STTEvent("eager_end_of_turn", text="Hey, what's the weather?"),
                      STTEvent("turn_resumed", text="Hey, what's the weather?"),
                      STTEvent("end_of_turn", text="Hey, what's the weather tomorrow?", confidence=1.0)]
    ws.push("turn.update", "2", transcript="and")  # a turn seen first in an update still starts
    await settle()
    assert [e.kind for e in events][6:] == ["start_of_turn", "partial"]
    await stream.aclose()


async def test_finish_ends_the_turn_here_and_reset_forgets_it():
    conn, events = Connector(), Events()
    stream = await _ink(conn).open(events)
    ws = conn.ws
    ws.push("turn.start")
    ws.push("turn.update", transcript="tell me a very long")
    await settle()
    await stream.finish()  # the session's max_turn_seconds: no "end now" in Ink, so it ends here
    ws.push("turn.end", transcript="tell me a very long story")  # its own end of that turn: ignored
    await settle()
    assert [e.kind for e in events] == ["start_of_turn", "partial", "end_of_turn"]
    assert events[-1].text == "tell me a very long"
    ws.push("turn.start", "2")
    ws.push("turn.update", "2", transcript="um")
    await settle()
    await stream.reset()  # LittleBot answered: the rest of that turn is forgotten
    ws.push("turn.end", "2", transcript="um okay")
    ws.push("turn.start", "3")
    await settle()
    assert [e.kind for e in events][3:] == ["start_of_turn", "partial", "start_of_turn"]
    await stream.aclose()


async def test_errors_and_gates():
    conn, events = Connector(), Events()
    stream = await _ink(conn).open(events)
    conn.ws.inbox.put_nowait(json.dumps({"type": "error", "status_code": 402, "error_code": "quota_exceeded",
                                         "message": "out of credits", "request_id": "r1"}))
    await settle()
    assert events == [STTEvent("error", detail="quota")]
    await stream.aclose()
    cloud = [True]
    ink = _ink(Connector(), cloud)
    assert ink.missing() is None
    cloud[0] = False
    assert ink.missing() == "cloud_off"
    assert CartesiaInk(key=lambda: "", cloud_ok=lambda: True, ws_connect=Connector()).missing() == "no_key"
    try:
        await _ink(Connector(error=TimeoutError())).open(Events())
    except SpeechError as e:
        assert e.kind == "timeout"
    else:
        raise AssertionError("expected SpeechError")
