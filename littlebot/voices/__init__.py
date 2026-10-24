"""Voices: who is talking, recognised by voice on the robot among the family a parent added (opt-in).

The same people as face recognition (Who's who on the dashboard's Profile page, littlebot/vision/faces.py),
the same privacy rules: off until a parent switches on "Recognise family voices"
(``privacy.voice_recognition_enabled``), only voiceprints (numbers) are kept, never recordings, and they
never leave the robot. See docs/VOICES.md.

* ``littlebot.voices.service``: ``VoiceRecognition``, the shared service (``ctx.voices``; ``get_voices(ctx)``):
  ``identify``, ``enrol``, ``learn``, ``forget``, ``status``;
* ``littlebot.voices.call``: ``CallVoices``, during a live voice call (who woke LittleBot, who is talking
  now, "I'm Ali", "do you know who I am?"); it publishes ``Topics.VOICE_SPEAKER`` ("voice.speaker") for each
  of their turns;
* ``littlebot.voices.engine``: the sherpa-onnx speaker-embedding model (optional: ``pip install -e '.[voices]'``
  and ``scripts/download_models.py voices``); ``littlebot.voices.store``: the voiceprints in the database.

``create_service(ctx)`` never loads the model or the package: that happens on first use, and everything
degrades to "not set up" (with what to install) when either is missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from littlebot.core.events import Topics

if TYPE_CHECKING:  # pragma: no cover
    from littlebot.voices.service import VoiceRecognition

VOICE_SPEAKER = Topics.VOICE_SPEAKER  # "voice.speaker": who is talking in a live call (see core/events.py)


def create_service(ctx: Any) -> VoiceRecognition:
    from littlebot.voices.service import get_voices  # noqa: PLC0415

    return get_voices(ctx)


def get_voices(ctx: Any) -> VoiceRecognition:
    from littlebot.voices.service import get_voices as _get  # noqa: PLC0415

    return _get(ctx)


__all__ = ["VOICE_SPEAKER", "create_service", "get_voices"]
