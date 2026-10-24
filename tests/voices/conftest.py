"""Shared fixtures for the voice-recognition tests: a context with voices switched on and a fake model."""

from __future__ import annotations

import pytest

from littlebot.testing import FakeSpeakerEngine, make_test_context
from littlebot.voices.service import VoiceRecognition


@pytest.fixture
def vctx():
    """A test context with "Recognise family voices" (and the microphone) switched on."""
    c = make_test_context()
    c.privacy.set(voice_recognition_enabled=True, microphone_enabled=True)
    yield c
    c.db.close()


@pytest.fixture
async def voices(vctx):
    """``ctx.voices`` with the fake model (speaker k is a tone; see littlebot.testing.FakeSpeakerEngine)."""
    v = VoiceRecognition(vctx, engine=FakeSpeakerEngine())
    vctx.voices = v
    await v.start()
    yield v
    await v.stop()
