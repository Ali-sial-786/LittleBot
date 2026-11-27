"""WhatsApp calls test kit: a fake WhatsApp page behind the browser tests' fake DevTools peer and fake Chromium
launcher, a fake vision that lends its camera, a clock the test moves, and ``Env`` (a CallsService on a test
context). No real Chromium, no network, nothing of the real WhatsApp anywhere.

Imported under this unique module name (the test folders have no ``__init__.py``).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "browser"))

from browser_testkit import (  # type: ignore[import-not-found]  # noqa: E402
    PAGE_SESSION,
    PAGE_TARGET,
    FakeDevTools,
    FakeLauncher,
)

from littlebot.android.windows import Window  # noqa: E402
from littlebot.calls.service import CallsService  # noqa: E402
from littlebot.core.events import Event, collect  # noqa: E402
from littlebot.testing import make_test_context  # noqa: E402

WHATSAPP = Window(app_id="chrome-web.whatsapp.com__-Default", title="WhatsApp", active=True)
WHATSAPP_BEHIND = Window(app_id="chrome-web.whatsapp.com__-Default", title="WhatsApp")
OTHER_FRONT = Window(app_id="foot", title="terminal", active=True)

POINTS = {"search_box": {"x": 120, "y": 60}, "video_call": {"x": 900, "y": 40}, "voice_call": {"x": 960, "y": 40},
          "end_call": {"x": 500, "y": 560}, "accept_call": {"x": 600, "y": 300}, "decline_call": {"x": 400, "y": 300}}
ROW = {"found": True, "x": 150, "y": 200}


class FakePage:
    """What the fake WhatsApp page "shows", answered to LittleBot's page scripts (recognised by what they hold).

    ``linked`` / ``qr``: the chat list or the QR code is there. ``has``: the things that can be found (keys of
    ``POINTS``). ``chats``: the names WhatsApp's search finds. ``call``: none | incoming | in_call. A press on the
    video / voice button starts a call (``starts``), on End or Decline ends it, on Accept takes it."""

    def __init__(self) -> None:
        self.linked = True
        self.qr = False
        self.has = {"search_box", "video_call", "voice_call"}
        self.chats = {"Ali Khan"}
        self.opened: str | None = None  # the chat that is open
        self.typed: list[str] = []
        self.call = "none"
        self.starts = True
        self.cover = True
        self.cover_states: list[dict[str, Any]] = []
        self.injected = 0
        self.ask: str | None = None
        self.clicks: list[tuple[float, float]] = []
        self.passes: list[bool] = []
        self.kinds: list[str] = []

    def points(self) -> dict[str, Any]:
        out = {k: v for k, v in POINTS.items() if k in self.has}
        if self.call == "incoming":
            out.update({k: POINTS[k] for k in ("accept_call", "decline_call")})
        elif self.call == "in_call":
            out["end_call"] = POINTS["end_call"]
        if self.opened is None:
            out.pop("video_call", None)
            out.pop("voice_call", None)
        return out

    def evaluate(self, expr: str) -> Any:
        import json  # noqa: PLC0415

        if "__littlebotCover" in expr:
            self.injected += 1
            self.cover = True
            return True
        if "const keys = {}" in expr:
            ask, self.ask = self.ask, None
            pts = self.points()
            return {"keys": {k: 0 for k in pts}, "points": pts, "linked": self.linked, "qr": self.qr and not self.linked,
                    "call": self.call, "container": -1, "videos": 0, "cover": self.cover, "covered": self.cover,
                    "holes": 0, "ask": ask}
        if "exact.push" in expr:
            name = json.loads(expr[expr.rindex("})(") + 3:-1])
            return ROW if name in self.chats and self.typed and self.typed[-1] == name else {"found": False}
        if "header: true" in expr:
            name = json.loads(expr[expr.rindex("})(") + 3:-1])
            return {"header": self.opened is not None, "match": self.opened == name}
        if "data-pe" in expr:
            self.passes.append(expr.rstrip().endswith("(true)"))
            return self.cover
        if "setAttribute('data-state', st)" in expr:
            if not self.cover:
                return False
            self.cover_states.append(json.loads(json.loads(expr[expr.rindex("})(") + 3:-1])))
            return True
        return None

    def click(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("type") != "mousePressed":
            return {}
        at = (params["x"], params["y"])
        self.clicks.append(at)
        hit = next((k for k, p in {**POINTS, "row": ROW}.items() if (p["x"], p["y"]) == at), None)
        if hit == "row":
            self.opened = self.typed[-1] if self.typed else None
        elif hit in ("video_call", "voice_call") and self.opened is not None:
            self.kinds.append(hit)
            if self.starts:
                self.call = "in_call"
        elif hit in ("end_call", "decline_call"):
            self.call = "none"
        elif hit == "accept_call" and self.call == "incoming":
            self.call = "in_call"
        return {}

    def type(self, params: dict[str, Any]) -> dict[str, Any]:
        self.typed.append(str(params.get("text")))
        return {}


class FakeVision:
    """Lends its camera (``lend_camera`` / ``return_camera``), like VisionService."""

    def __init__(self) -> None:
        self.lent: set[str] = set()
        self.log: list[tuple[str, str]] = []

    async def lend_camera(self, owner: str, *, timeout: float = 3.0) -> bool:
        self.lent.add(owner)
        self.log.append(("lend", owner))
        return True

    def return_camera(self, owner: str) -> bool:
        had = owner in self.lent
        self.lent.discard(owner)
        self.log.append(("return", owner))
        return had

    def status(self) -> dict[str, Any]:
        return {"camera": "lent" if self.lent else "ok", "detail": ""}


class Clock:
    """A clock the test moves (``advance``); ``tick`` > 0 moves it that much every time it is read."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t
        self.tick = 0.0

    def __call__(self) -> float:
        self.t += self.tick
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@dataclass
class Env:
    ctx: Any
    calls: CallsService
    devtools: FakeDevTools
    launcher: FakeLauncher
    clock: Clock
    page: FakePage
    vision: FakeVision
    tmp: Path
    memory: list[int | None]
    windows: list[Any]
    events: list[Event] = field(default_factory=list)
    said: list[Event] = field(default_factory=list)

    @property
    def profile(self) -> Path:
        return self.tmp / "whatsapp-profile"

    def last(self, topic: str) -> Event | None:
        found = [e for e in self.events if e.topic == topic]
        return found[-1] if found else None

    def words(self) -> list[str]:
        return [str(e.data.get("text")) for e in self.said]

    async def settle(self, n: int = 5) -> None:
        for _ in range(n):
            await asyncio.sleep(0.01)
        await self.calls._wait_teardowns()

    async def until(self, cond: Callable[[], Any], timeout: float = 3.0) -> None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            if loop.time() > end:
                raise AssertionError("condition not met in time")
            await asyncio.sleep(0.01)

    async def open(self, **kw: Any) -> dict[str, Any]:
        kw.setdefault("source", "voice")
        return await self.calls.open(**kw)


async def build_env(tmp: Path, *, launcher_kw: dict[str, Any] | None = None, calls_kw: dict[str, Any] | None = None,
                    contacts: dict[str, str] | None = None, **config: Any) -> Env:
    ctx = make_test_context(**config)
    ctx.privacy.set(cloud_enabled=True)
    ctx.settings.set("calls.contacts", {"Dad": "Ali Khan", "Mum": "Sara"} if contacts is None else contacts)
    devtools = await FakeDevTools().start()
    page = FakePage()
    devtools.eval_value = page.evaluate
    devtools.answers["Input.dispatchMouseEvent"] = page.click
    devtools.answers["Input.insertText"] = page.type
    launcher = FakeLauncher(devtools, **(launcher_kw or {}))
    clock = Clock()
    memory: list[int | None] = [2000]
    windows: list[Any] = [[WHATSAPP]]
    vision = FakeVision()
    ctx.vision = vision
    kw: dict[str, Any] = {"launcher": launcher, "browser_path": "/usr/bin/chromium", "clock": clock, "kiosk": True,
                          "profile_dir": tmp / "whatsapp-profile", "memory": lambda: memory[0],
                          "windows": lambda: windows[0], "poll": 0.01, "step_poll": 0.005, "settle": 0.0,
                          "proc_root": str(tmp / "no-proc")}
    kw.update(calls_kw or {})
    calls = CallsService(ctx, **kw)
    ctx.calls = calls
    env = Env(ctx=ctx, calls=calls, devtools=devtools, launcher=launcher, clock=clock, page=page, vision=vision,
              tmp=tmp, memory=memory, windows=windows)
    env.events = collect(ctx.bus, ["calls.*"])
    env.said = collect(ctx.bus, ["assistant.announce"])
    await calls.start()
    return env


async def close_env(env: Env) -> None:
    await env.calls.stop()
    await env.devtools.stop()
    env.ctx.db.close()


__all__ = ["OTHER_FRONT", "PAGE_SESSION", "PAGE_TARGET", "POINTS", "ROW", "WHATSAPP", "WHATSAPP_BEHIND", "Clock",
           "Env", "FakePage", "FakeVision", "build_env", "close_env"]
