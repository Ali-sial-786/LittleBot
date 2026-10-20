"""Audio subsystem: speaker/microphone devices, sound effects, TTS/STT, half-duplex AudioService.

``create_service(ctx)`` returns an ``AudioService`` (implements ``core.interfaces.AudioAPI``).
Heavy / optional dependencies (sounddevice, pygame, vosk, httpx) are imported lazily.
"""

from __future__ import annotations

from typing import Any


def create_service(ctx: Any) -> Any:
    from littlebot.audio.service import AudioService  # noqa: PLC0415 - keep package import cheap

    return AudioService(ctx)


__all__ = ["create_service"]
