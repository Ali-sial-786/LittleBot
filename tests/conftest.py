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
