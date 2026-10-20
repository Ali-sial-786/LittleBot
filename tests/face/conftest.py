"""Fixtures for the face tests (headless SDL is configured by tests/conftest.py)."""

from __future__ import annotations

import pytest

from littlebot.face.controller import FaceController
from littlebot.testing import make_test_context


@pytest.fixture
def face_ctx():
    """Context without fakes (so ctx.face can be the real controller)."""
    c = make_test_context(fakes=False)
    yield c
    c.db.close()


@pytest.fixture
def controller(face_ctx):
    fc = FaceController(face_ctx, seed=1)
    face_ctx.face = fc
    yield fc
    fc.stop_ticker()


@pytest.fixture
def pygame_display():
    """Initialised pygame display (dummy driver) that is shut down afterwards."""
    import pygame

    pygame.display.init()
    pygame.font.init()
    yield pygame
    pygame.display.quit()
