"""Helpers for the screens tests: a dummy card under a test-only kind, a fake clock, a service fixture.

(Fixtures live here rather than in a conftest so they can't clash with other screens tests; test modules
import the ones they use.)
"""

from __future__ import annotations

from typing import Any

import pytest

from littlebot.screens import base
from littlebot.screens.base import Card, CardType
from littlebot.testing import make_test_context

KIND = "test_dummy"  # registered only while a test runs
CARD_RGB = (40, 160, 90)  # the dummy card paints its whole canvas this colour


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class DummyCard(Card):
    """Paints the canvas green; records taps, keys and commands; "next" pages, "finish" sets done."""

    kind = KIND

    def __init__(self, spec: dict[str, Any], host: Any = None) -> None:
        super().__init__(spec, host)
        self.sticky = bool(spec.get("sticky"))
        self.page = 0
        self.taps: list[tuple[float, float]] = []
        self.keys: list[str] = []
        self.draws = 0
        self.boom = bool(spec.get("boom"))
        self.closed = False

    def draw(self, surf: Any) -> None:
        if self.boom:
            raise RuntimeError("broken card")
        self.draws += 1
        surf.fill(CARD_RGB)

    def tap(self, x: float, y: float) -> bool:
        self.taps.append((x, y))
        return True

    def key(self, name: str) -> bool:
        self.keys.append(name)
        return name == "right"

    def command(self, action: str, args: dict[str, Any]) -> bool:
        if action == "next":
            self.page += 1
            return True
        if action == "finish":
            self.done = True
            return True
        return False

    def describe(self) -> str:
        return f"{self.title} (page {self.page + 1})"

    def close(self) -> None:
        self.closed = True


async def _build(spec: dict[str, Any], ctx: Any, host: Any) -> Card | None:
    if spec.get("empty"):
        return None
    return DummyCard(spec, host)


@pytest.fixture
def dummy_kind():
    base.card_types()  # register the built-in ones first, so ours is simply added
    base.register(CardType(kind=KIND, description="a test card", fields={"title": {"type": "string"}},
                           build=_build, required=("title",), actions={"next": "the next page"}))
    yield KIND
    base._TYPES.pop(KIND, None)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def sctx():
    c = make_test_context()
    yield c
    c.db.close()


@pytest.fixture
def svc(sctx, clock, dummy_kind):
    from littlebot.screens.service import ScreenService

    s = ScreenService(sctx, clock=clock)
    sctx.screens = s
    yield s
    s.detach()


def events(ctx: Any, pattern: str) -> list[Any]:
    """Start recording bus events matching ``pattern``; returns the list they land in."""
    got: list[Any] = []
    ctx.bus.subscribe(pattern, got.append)
    return got
