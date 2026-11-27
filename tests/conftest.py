"""Shared pytest configuration: headless SDL, deterministic contexts."""

from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pytest

from littlebot.testing import make_test_context


@pytest.fixture
def ctx():
    """A RobotContext with in-memory storage, fake face/audio/motion and instant activity timing."""
    c = make_test_context()
    yield c
    c.db.close()


@pytest.fixture(autouse=True)
def _two_gb_board(monkeypatch):
    """Every test runs as on a 2 GB Pi (``system.memory: auto`` -> small), whatever this computer has; tests of
    what a 4 GB one keeps set ``system.memory`` or patch ``littlebot.core.memory.total_mb`` themselves."""
    from littlebot.core import memory  # noqa: PLC0415

    monkeypatch.setattr(memory, "total_mb", lambda: 2000)
