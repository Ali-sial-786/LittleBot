"""Fixtures for activity tests (helpers live in activity_helpers.py)."""

from __future__ import annotations

import pytest

from activity_helpers import LoggingAudio, LoggingFace, Timeline
from littlebot.testing import make_test_context


@pytest.fixture
def timeline_ctx():
    """Context whose fake face/audio write into one shared Timeline (``ctx.extras['timeline']``)."""
    c = make_test_context()
    tl = Timeline()
    c.face = LoggingFace(c.bus, tl)
    c.audio = LoggingAudio(c.bus, tl)
    c.extras["timeline"] = tl
    yield c
    c.db.close()


@pytest.fixture
def slow_ctx():
    """Real-time clock (time_scale 1) for tests that need an activity to stay 'in progress'."""
    c = make_test_context(activities={"time_scale": 1.0})
    yield c
    c.db.close()
