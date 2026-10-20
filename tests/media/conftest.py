"""Fixtures for the media (YouTube playback) tests: fake display backend, manual clock, fixed day."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from littlebot.core.clock import ManualClock
from littlebot.core.context import RobotContext
from littlebot.core.events import Event, Topics, collect
from littlebot.media.controller import MediaController
from littlebot.media.display import DisplayBackend, PlayerBackendError
from littlebot.testing import make_test_context

VID_A = "dQw4w9WgXcQ"
VID_B = "M7lc1UVf-VE"
VID_C = "aqz-KE-bpKQ"
LIST_A = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"


class FakeBackend(DisplayBackend):
    """Records open/close; behaves like the kiosk (closable, knows whether its window runs)."""

    def __init__(self, *, name: str = "kiosk", can_close: bool = True, fail: str | None = None) -> None:
        super().__init__()
        self.name = name
        self.can_close = can_close
        self.fail = fail
        self.opened: list[str] = []
        self.closed = 0
        self.running = False

    async def open(self, url: str) -> None:
        if self.fail:
            raise PlayerBackendError(self.fail)
        self.opened.append(url)
        self.running = True

    async def close(self) -> None:
        self.closed += 1
        self.running = False

    def is_running(self) -> bool | None:
        return self.running if self.can_close else None


@dataclass
class MediaEnv:
    ctx: RobotContext
    media: MediaController
    clock: ManualClock
    backend: FakeBackend
    day: list[dt.date]
    events: list[Event] = field(default_factory=list)

    def topics(self, prefix: str = "") -> list[str]:
        return [e.topic for e in self.events if e.topic.startswith(prefix)]

    def last(self, topic: str) -> Event | None:
        found = [e for e in self.events if e.topic == topic]
        return found[-1] if found else None

    def all(self, topic: str) -> list[Event]:
        return [e for e in self.events if e.topic == topic]

    def add(self, vid: str = VID_A, title: str | None = "Song", **kw: Any) -> dict[str, Any]:
        return self.media.add_item(f"https://www.youtube.com/watch?v={vid}", title, **kw)

    def face_states(self) -> list[str]:
        """Face state requested by each RobotState change (the face owns face.visible)."""
        return [e.data["face_state"] for e in self.events if e.topic == Topics.STATE_CHANGED]

    def poll(self) -> dict[str, Any]:
        """Act like the player page polling the bridge."""
        return self.media.player_state()

    async def page_event(self, type_: str, **extra: Any) -> dict[str, Any]:
        state = self.media.player_state()
        return await self.media.handle_player_event({"type": type_, "load_id": state["load_id"], **extra})

    async def settle(self, n: int = 5) -> None:
        for _ in range(n):
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)


def _build(
    *, age: int | None = 24, backend: FakeBackend | None = None, tick: float = 3600.0,
    media_cfg: dict[str, Any] | None = None, mode: str = "simulation", ctx: RobotContext | None = None,
    **controller_kw: Any,
) -> MediaEnv:
    cfg = {"player": "none", "connect_timeout_seconds": 1e6, "heartbeat_timeout_seconds": 1e6,
           "kiosk_close_delay_seconds": 0.0}
    cfg.update(media_cfg or {})
    if ctx is None:
        ctx = make_test_context(mode=mode, media=cfg)
        if age is not None:
            ctx.settings.set("profile", {"age_months": age})
    clock = controller_kw.pop("clock", None) or ManualClock(1000.0)
    day = controller_kw.pop("day", None) or [dt.date(2026, 10, 20)]
    backend = backend or FakeBackend()
    media = MediaController(ctx, backend=backend, clock=clock, today=lambda: day[0], tick_seconds=tick,
                            **controller_kw)
    env = MediaEnv(ctx=ctx, media=media, clock=clock, backend=backend, day=day)
    env.events = collect(ctx.bus, ["media.*", "face.*", "state.*"])
    return env


@pytest.fixture
def make_env() -> Any:
    envs: list[MediaEnv] = []

    def factory(**kw: Any) -> MediaEnv:
        env = _build(**kw)
        envs.append(env)
        return env

    yield factory
    for env in envs:
        env.ctx.db.close()


@pytest.fixture
async def env(make_env: Callable[..., MediaEnv]) -> Any:
    e = make_env()
    await e.media.start()
    yield e
    await e.media.close()


@pytest.fixture
def fake_backend_cls() -> type[FakeBackend]:
    return FakeBackend
