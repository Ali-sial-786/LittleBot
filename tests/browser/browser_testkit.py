"""Browser view test kit: a fake DevTools peer (a real localhost WebSocket server), a fake Chromium launcher,
a manual clock and ``Env`` (a BrowserView on a test context). No real Chromium anywhere.

Imported under this unique module name (the test folders have no ``__init__.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from littlebot.browser.cdp import (
    OP_CLOSE,
    OP_PING,
    OP_TEXT,
    accept_key,
    encode_frame,
    read_frame,
)
from littlebot.browser.service import BrowserView
from littlebot.core.events import Event, collect
from littlebot.testing import make_test_context

PAGE_TARGET = "T-PAGE"
PAGE_SESSION = "S-PAGE"
CONTEXT_ID = "C-INCOGNITO"


class FakeDevTools:
    """Answers the DevTools methods the browser view uses, over a real WebSocket on 127.0.0.1.

    ``calls``: every (method, params, sessionId) received. ``answers[method]``: a dict or ``fn(params)`` returning
    one (or raising ``Refuse`` for an error answer). ``eval_value(expression)``: what ``Runtime.evaluate`` returns.
    ``emit(method, params, session)`` sends an event."""

    class Refuse(Exception):
        pass

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.answers: dict[str, Any] = {}
        self.eval_value: Callable[[str], Any] = lambda expr: None
        self.history: dict[str, Any] = {"currentIndex": 0, "entries": [{"id": 1, "url": "data:text/html,loading"}]}
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self.handshakes = 0
        self.paths: list[str] = []

    async def start(self) -> FakeDevTools:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for w in self._writers:
            w.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def drop(self) -> None:
        """The connection goes away (Chromium crashed)."""
        for w in list(self._writers):
            w.close()
        self._writers.clear()

    # ------------------------------------------------------------ what was asked
    def methods(self, session: str | None | type = ...) -> list[str]:
        return [m for m, _p, s in self.calls if session is ... or s == session]

    def params(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p, _s in self.calls if m == method]

    def last(self, method: str) -> dict[str, Any] | None:
        found = self.params(method)
        return found[-1] if found else None

    def count(self, method: str) -> int:
        return len(self.params(method))

    # ------------------------------------------------------------ events
    async def emit(self, method: str, params: dict[str, Any] | None = None, session: str | None = PAGE_SESSION
                   ) -> None:
        msg: dict[str, Any] = {"method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        await self._send_all(json.dumps(msg))

    async def ping(self) -> None:
        for w in list(self._writers):
            w.write(encode_frame(OP_PING, b"hi", mask=False))
            await w.drain()

    async def _send_all(self, text: str) -> None:
        for w in list(self._writers):
            w.write(encode_frame(OP_TEXT, text.encode("utf-8"), mask=False))
            await w.drain()

    # ------------------------------------------------------------ the peer
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        lines = raw.split("\r\n")
        self.paths.append(lines[0].split(" ")[1])
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(headers['sec-websocket-key'])}\r\n\r\n").encode("ascii"))
        await writer.drain()
        self.handshakes += 1
        self._writers.append(writer)
        try:
            while True:
                _fin, op, payload = await read_frame(reader)
                if op == OP_CLOSE:
                    break
                if op != OP_TEXT:
                    continue
                msg = json.loads(payload.decode("utf-8"))
                reply = self._answer(msg)
                writer.write(encode_frame(OP_TEXT, json.dumps(reply).encode("utf-8"), mask=False))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()

    def _answer(self, msg: dict[str, Any]) -> dict[str, Any]:
        method, params, session = msg["method"], msg.get("params") or {}, msg.get("sessionId")
        self.calls.append((method, params, session))
        try:
            result = self._result(method, params)
        except FakeDevTools.Refuse as e:
            return {"id": msg["id"], "error": {"code": -32000, "message": str(e) or "refused"}}
        return {"id": msg["id"], "result": result}

    def _result(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        answer = self.answers.get(method)
        if callable(answer):
            return answer(params)
        if answer is not None:
            return answer
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "T-SW", "type": "service_worker"},
                                    {"targetId": PAGE_TARGET, "type": "page", "browserContextId": CONTEXT_ID,
                                     "url": "data:text/html,loading"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": PAGE_SESSION}
        if method == "Browser.getVersion":
            return {"product": "Chrome/141.0.7390.37"}
        if method == "Page.navigate":
            return {"frameId": PAGE_TARGET, "loaderId": "L1"}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 42}
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "value": self.eval_value(params.get("expression", ""))}}
        if method == "Page.getNavigationHistory":
            return self.history
        return {}


class FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._rc: int | None = None
        self.terminated = 0
        self._done = asyncio.Event()

    @property
    def returncode(self) -> int | None:
        return self._rc

    def exit(self, rc: int = 0) -> None:
        self._rc = rc
        self._done.set()

    def terminate(self) -> None:
        self.terminated += 1
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)

    async def wait(self) -> int:
        await self._done.wait()
        return int(self._rc or 0)


class FakeLauncher:
    """Starts "Chromium": records the command line and writes DevToolsActivePort into its --user-data-dir,
    pointing at the fake DevTools peer (``write_port=False``: it never does, like a Chromium that hangs)."""

    def __init__(self, devtools: FakeDevTools, *, write_port: bool = True, fail: Exception | None = None) -> None:
        self.devtools = devtools
        self.write_port = write_port
        self.fail = fail
        self.argvs: list[list[str]] = []
        self.procs: list[FakeProcess] = []

    async def __call__(self, argv: list[str]) -> FakeProcess:
        self.argvs.append(list(argv))
        if self.fail is not None:
            raise self.fail
        data_dir = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir=")))
        if self.write_port:
            (data_dir / "DevToolsActivePort").write_text(f"{self.devtools.port}\n/devtools/browser/fake-id\n")
        proc = FakeProcess(4242 + len(self.procs))
        self.procs.append(proc)
        return proc

    @property
    def argv(self) -> list[str]:
        return self.argvs[-1]

    @property
    def proc(self) -> FakeProcess:
        return self.procs[-1]


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@dataclass
class Env:
    ctx: Any
    browser: BrowserView
    devtools: FakeDevTools
    launcher: FakeLauncher
    clock: Clock
    tmp: Path
    events: list[Event] = field(default_factory=list)

    def topics(self, prefix: str = "") -> list[str]:
        return [e.topic for e in self.events if e.topic.startswith(prefix)]

    def last(self, topic: str) -> Event | None:
        found = [e for e in self.events if e.topic == topic]
        return found[-1] if found else None

    def profiles(self) -> list[Path]:
        return sorted(self.tmp.glob("littlebot-browser-*"))

    async def settle(self, n: int = 3) -> None:
        for _ in range(n):
            await asyncio.sleep(0.01)
        await self.browser._wait_teardowns()

    async def until(self, cond: Callable[[], bool], timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            if loop.time() > end:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(0.01)

    async def open(self, query: str | None = "how tall are giraffes", **kw: Any) -> dict[str, Any]:
        kw.setdefault("source", "voice")
        return await self.browser.open(query, **kw)


async def build_env(tmp: Path, *, cloud: bool = True, launcher_kw: dict[str, Any] | None = None,
                    browser_kw: dict[str, Any] | None = None, **config: Any) -> Env:
    ctx = make_test_context(**config)
    if cloud:
        ctx.privacy.set(cloud_enabled=True)
    devtools = await FakeDevTools().start()
    launcher = FakeLauncher(devtools, **(launcher_kw or {}))
    clock = Clock()
    kw: dict[str, Any] = {"launcher": launcher, "browser_path": "/usr/bin/chromium", "clock": clock,
                          "kiosk": True, "tmp_root": tmp, "profile_dir": tmp / "saved-browser"}
    kw.update(browser_kw or {})
    browser = BrowserView(ctx, **kw)
    ctx.browser = browser
    env = Env(ctx=ctx, browser=browser, devtools=devtools, launcher=launcher, clock=clock, tmp=tmp)
    env.events = collect(ctx.bus, ["browser.*", "screen.*"])
    await browser.start()
    return env


async def close_env(env: Env) -> None:
    await env.browser.stop()
    await env.devtools.stop()
    env.ctx.db.close()
