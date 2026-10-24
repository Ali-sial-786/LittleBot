"""The standard-library WebSocket / DevTools client (littlebot/browser/cdp.py) against the fake DevTools peer."""

from __future__ import annotations

import asyncio
import json

import pytest
from browser_testkit import FakeDevTools  # type: ignore[import-not-found]

from littlebot.browser.cdp import (
    OP_TEXT,
    CDPConnection,
    CDPError,
    WebSocket,
    WebSocketError,
    accept_key,
    encode_frame,
    read_frame,
)


async def _frames(data: bytes) -> list[tuple[bool, int, bytes]]:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    out = []
    while True:
        try:
            out.append(await read_frame(reader))
        except asyncio.IncompleteReadError:
            return out


@pytest.mark.parametrize("size", [0, 5, 125, 126, 300, 65535, 65536, 70000])
async def test_frames_round_trip_masked_or_not(size: int) -> None:
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    for mask in (True, False):
        frame = encode_frame(OP_TEXT, payload, mask=mask)
        assert bool(frame[1] & 0x80) is mask
        assert await _frames(frame) == [(True, OP_TEXT, payload)]


def test_accept_key_matches_rfc6455() -> None:
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


async def test_only_this_computer_is_ever_connected() -> None:
    for url in ("ws://example.com:9222/devtools", "wss://127.0.0.1:9222/x", "http://127.0.0.1:9222/",
                "ws://127.0.0.1/no-port", "ws://192.168.1.2:9222/x"):
        with pytest.raises(WebSocketError):
            await WebSocket.connect(url)


async def test_commands_events_and_errors() -> None:
    peer = await FakeDevTools().start()
    try:
        conn = await CDPConnection.connect(f"ws://127.0.0.1:{peer.port}/devtools/browser/x")
        events: list[tuple[str, dict, str | None]] = []
        conn.on_event(lambda m, p, s: events.append((m, p, s)))
        assert await conn.send("Target.attachToTarget", {"targetId": "T"}) == {"sessionId": "S-PAGE"}
        assert (await conn.send("Page.navigate", {"url": "https://a.example/"}, session="S-PAGE"))["frameId"]
        assert peer.calls[-1] == ("Page.navigate", {"url": "https://a.example/"}, "S-PAGE")
        peer.answers["Page.reload"] = lambda p: (_ for _ in ()).throw(FakeDevTools.Refuse("no page"))
        with pytest.raises(CDPError, match="no page"):
            await conn.send("Page.reload")
        await peer.ping()  # answered, never mistaken for a message
        big = "x" * 100_000
        await peer.emit("Runtime.consoleAPICalled", {"text": big}, session="S-PAGE")
        await asyncio.sleep(0.05)
        assert events == [("Runtime.consoleAPICalled", {"text": big}, "S-PAGE")]
        many = await asyncio.gather(*(conn.send("Browser.getVersion") for _ in range(20)))
        assert all(r == {"product": "Chrome/141.0.7390.37"} for r in many)
        await peer.drop()
        await asyncio.wait_for(conn.closed.wait(), 2)
        with pytest.raises(CDPError):
            await conn.send("Browser.getVersion")
        await conn.close()
    finally:
        await peer.stop()


async def test_a_bad_event_handler_does_not_stop_the_reader() -> None:
    peer = await FakeDevTools().start()
    try:
        conn = await CDPConnection.connect(f"ws://127.0.0.1:{peer.port}/devtools/browser/x")
        conn.on_event(lambda m, p, s: 1 / 0)
        await peer.emit("Page.loadEventFired", {})
        assert await conn.send("Browser.getVersion")
        await conn.close()
        assert conn.closed.is_set()
    finally:
        await peer.stop()


async def test_a_call_without_an_answer_times_out() -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        key = next(ln.split(":", 1)[1].strip() for ln in raw.split("\r\n") if ln.lower().startswith("sec-websocket-key"))
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode())
        await writer.drain()
        await asyncio.sleep(5)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        conn = await CDPConnection.connect(f"ws://127.0.0.1:{port}/x")
        with pytest.raises(CDPError, match="no answer"):
            await conn.send("Browser.getVersion", timeout=0.1)
        await conn.close()
    finally:
        server.close()


async def test_a_wrong_handshake_is_refused() -> None:
    async def liar(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: nope\r\n\r\n")
        await writer.drain()

    server = await asyncio.start_server(liar, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(WebSocketError, match="accept key"):
            await WebSocket.connect(f"ws://127.0.0.1:{port}/x")
    finally:
        server.close()


def test_messages_are_json() -> None:
    assert json.loads(encode_frame(OP_TEXT, b'{"id":1}', mask=False)[2:]) == {"id": 1}
