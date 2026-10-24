"""Helpers for the voice-recognition tests (the fake model: littlebot.testing.FakeSpeakerEngine)."""

from __future__ import annotations

from typing import Any

from littlebot.testing import speaker_tone


def add_person(voices: Any, name: str, role: str = "") -> int:
    return int(voices.store.people.add_person(name, role)["id"])


async def enrol(voices: Any, person_id: int, speaker: int, seconds: float = 12.0) -> dict[str, Any]:
    """The dashboard's recording: ``speaker`` reading for ``seconds`` (48 kHz int16, like the microphone)."""
    return await voices.enrol(person_id, speaker_tone(speaker, seconds, 48000, int16=True), 48000)


def say(speaker: int, seconds: float, rate: int = 48000, **kw: Any):
    """What ``speaker`` saying something for ``seconds`` sounds like to the call (48 kHz int16)."""
    return speaker_tone(speaker, seconds, rate, int16=True, **kw)
