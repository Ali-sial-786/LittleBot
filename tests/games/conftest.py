"""Fixtures for the games tests: a fake clock, a recording host, a games service on a fake context."""

from __future__ import annotations

import pytest

from games_helpers import FakeClock
from littlebot.games.base import RecordingHost
from littlebot.games.service import GamesService
from littlebot.testing import make_test_context


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def host(clock):
    return RecordingHost(clock=clock)


@pytest.fixture
def ctx():
    c = make_test_context()
    yield c
    c.db.close()


@pytest.fixture
def svc(ctx, clock):
    s = GamesService(ctx, clock=clock, seed=7)
    ctx.games = s
    yield s
    s.close()


@pytest.fixture
def surface():
    import pygame

    return pygame.Surface((720, 480))
