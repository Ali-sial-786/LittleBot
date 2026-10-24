"""A small Chrome DevTools Protocol client over a localhost WebSocket, using only the standard library.

LittleBot's browser view (``littlebot.browser.service``) drives its Chromium window through DevTools. The
robot's install has no WebSocket package (httpx has none, aiortc's dependencies have none, and ``websockets``
is not one of LittleBot's dependencies), so this module speaks the little of RFC 6455 that DevTools needs:
the opening handshake, masked client frames, text messages (fragmented or not), ping / pong and close. It only
ever connects to ``127.0.0.1`` (``connect`` refuses any other host).

* ``WebSocket``      - one client connection (``send_text``, ``recv`` -> str | None at close, ``close``)
* ``CDPConnection``  - DevTools on top of it: ``await send(method, params, session=...)`` -> the result dict
                       (raises ``CDPError``), ``on_event(callback)`` for events, ``closed``
* ``encode_frame`` / ``read_frame`` - the framing, also used by the tests' fake DevTools peer

Everything runs on the asyncio loop; every call has a timeout.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import logging
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("littlebot.browser.cdp")

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455's fixed key suffix
MAX_MESSAGE = 16 * 1024 * 1024  # bytes; DevTools messages here are a few KB
CONNECT_TIMEOUT_S = 5.0
CALL_TIMEOUT_S = 10.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

EventCallback = Callable[[str, dict[str, Any], "str | None"], Any]


class WebSocketError(ConnectionError):
    """The WebSocket handshake failed or the peer broke the protocol."""


class CDPError(RuntimeError):
    """DevTools answered a command with an error (or the connection went away)."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- framing
def _mask(payload: bytes, key: bytes) -> bytes:
    if not payload:
        return payload
    n = len(payload)
    keys = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(keys, "big")).to_bytes(n, "big")


def encode_frame(opcode: int, payload: bytes, *, mask: bool, fin: bool = True) -> bytes:
    """One frame. Clients mask what they send (``mask=True``); servers don't."""
    head = bytearray([(0x80 if fin else 0) | (opcode & 0x0F)])
    n = len(payload)
    bit = 0x80 if mask else 0
    if n < 126:
        head.append(bit | n)
    elif n < 65536:
        head.append(bit | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(bit | 127)
        head += n.to_bytes(8, "big")
    if mask:
        key = os.urandom(4)
        return bytes(head) + key + _mask(payload, key)
    return bytes(head) + payload


async def read_frame(reader: asyncio.StreamReader, *, max_size: int = MAX_MESSAGE) -> tuple[bool, int, bytes]:
    """(fin, opcode, payload) of the next frame (masked or not). Raises IncompleteReadError at EOF."""
    b0, b1 = await reader.readexactly(2)
    fin, opcode = bool(b0 & 0x80), b0 & 0x0F
    if b0 & 0x70:
        raise WebSocketError("unexpected WebSocket extension bits")
    masked, n = bool(b1 & 0x80), b1 & 0x7F
    if n == 126:
        n = int.from_bytes(await reader.readexactly(2), "big")
    elif n == 127:
        n = int.from_bytes(await reader.readexactly(8), "big")
    if n > max_size:
        raise WebSocketError(f"WebSocket frame too big ({n} bytes)")
    key = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(n) if n else b""
    return fin, opcode, _mask(payload, key) if masked else payload


def accept_key(key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


# ---------------------------------------------------------------- the client
class WebSocket:
    """One client connection (text messages only, which is all DevTools uses)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader, self._writer = reader, writer
        self._send_lock = asyncio.Lock()
        self.closed = False

    @classmethod
    async def connect(cls, url: str, *, timeout: float = CONNECT_TIMEOUT_S) -> WebSocket:
        """Open ``ws://127.0.0.1:<port>/<path>`` (any other host is refused)."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.scheme != "ws" or host not in LOCAL_HOSTS or not parts.port:
            raise WebSocketError("only ws:// connections to this computer are allowed")
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, parts.port), timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            hostport = f"[{host}]:{parts.port}" if ":" in host else f"{host}:{parts.port}"
            writer.write((f"GET {path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                         .encode("ascii"))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            if len(raw) > 16384:
                raise WebSocketError("WebSocket handshake answer too long")
            lines = raw.decode("latin-1").split("\r\n")
            status = lines[0].split(" ", 2)
            if len(status) < 2 or status[1] != "101":
                raise WebSocketError(f"WebSocket handshake refused ({lines[0][:80]})")
            headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            if headers.get("sec-websocket-accept") != accept_key(key):
                raise WebSocketError("WebSocket handshake: wrong accept key")
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer)

    async def send_text(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("the WebSocket is closed")
        async with self._send_lock:
            self._writer.write(encode_frame(OP_TEXT, text.encode("utf-8"), mask=True))
            await self._writer.drain()

    async def recv(self) -> str | None:
        """The next text message, or None once the connection is closed."""
        parts: list[bytes] = []
        size = 0
        while not self.closed:
            try:
                fin, op, payload = await read_frame(self._reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                self.closed = True
                return None
            if op == OP_PING:
                with contextlib.suppress(Exception):
                    async with self._send_lock:
                        self._writer.write(encode_frame(OP_PONG, payload, mask=True))
                        await self._writer.drain()
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                await self.close()
                return None
            if op in (OP_TEXT, OP_BINARY, OP_CONT):
                size += len(payload)
                if size > MAX_MESSAGE:
                    raise WebSocketError("WebSocket message too big")
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", errors="replace")
        return None

    async def close(self) -> None:
        if self.closed and self._writer.is_closing():
            return
        self.closed = True
        with contextlib.suppress(Exception):
            self._writer.write(encode_frame(OP_CLOSE, b"\x03\xe8", mask=True))  # 1000: normal closure
            await asyncio.wait_for(self._writer.drain(), 1.0)
        with contextlib.suppress(Exception):
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), 1.0)


class CDPConnection:
    """DevTools over one WebSocket: commands with ids, events to callbacks (on the loop, kept tiny)."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._callbacks: list[EventCallback] = []
        self._reader: asyncio.Task[Any] | None = None
        self.closed = asyncio.Event()

    @classmethod
    async def connect(cls, url: str, *, timeout: float = CONNECT_TIMEOUT_S) -> CDPConnection:
        conn = cls(await WebSocket.connect(url, timeout=timeout))
        conn._reader = asyncio.get_running_loop().create_task(conn._read(), name="browser-devtools")
        return conn

    def on_event(self, callback: EventCallback) -> None:
        self._callbacks.append(callback)

    async def send(self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None,
                   timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        if self.closed.is_set():
            raise CDPError("the DevTools connection is closed")
        msg_id = next(self._ids)
        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send_text(json.dumps(msg, separators=(",", ":")))
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:  # (before OSError: TimeoutError is one)
            raise CDPError(f"{method}: no answer in {timeout:g} s") from e
        except (ConnectionError, OSError) as e:
            raise CDPError(f"the DevTools connection failed ({type(e).__name__})") from e
        finally:
            self._pending.pop(msg_id, None)

    async def _read(self) -> None:
        try:
            while True:
                text = await self._ws.recv()
                if text is None:
                    break
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg:
                    fut = self._pending.get(msg["id"])
                    if fut is not None and not fut.done():
                        err = msg.get("error")
                        if isinstance(err, dict):
                            fut.set_exception(CDPError(str(err.get("message") or "error")[:200], err.get("code")))
                        else:
                            fut.set_result(msg.get("result") or {})
                    continue
                method = msg.get("method")
                if isinstance(method, str):
                    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                    for cb in list(self._callbacks):
                        try:
                            cb(method, params, msg.get("sessionId"))
                        except Exception:  # noqa: BLE001 - one bad handler never stops the reader
                            log.exception("DevTools event handler failed (%s)", method)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.info("DevTools connection ended (%s)", type(e).__name__)
        finally:
            self._finish()

    def _finish(self) -> None:
        self.closed.set()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(CDPError("the DevTools connection closed"))
        self._pending.clear()

    async def close(self) -> None:
        await self._ws.close()
        task, self._reader = self._reader, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._finish()


__all__ = ["CDPConnection", "CDPError", "WebSocket", "WebSocketError", "accept_key", "encode_frame", "read_frame"]
