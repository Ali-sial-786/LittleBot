"""Text-to-speech providers: mock, espeak-ng, piper (local) and OpenAI (cloud, gated).

``build_tts`` returns ``(primary, local)``. The AudioService uses ``primary``
when allowed and falls back to ``local`` when the primary is a cloud provider
that is not allowed right now (privacy) or fails (timeout, invalid key...).
``auto`` never selects a cloud provider.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from littlebot.audio.tts.base import TTSProvider, clamp_rate, clean_text, estimate_seconds
from littlebot.audio.tts.espeak import EspeakTTS
from littlebot.audio.tts.mock import MockTTS
from littlebot.audio.tts.openai import OpenAITTS
from littlebot.audio.tts.piper import PiperTTS

log = logging.getLogger("littlebot.audio.tts")

__all__ = ["TTSProvider", "MockTTS", "EspeakTTS", "PiperTTS", "OpenAITTS", "build_tts", "local_tts",
           "clamp_rate", "clean_text", "estimate_seconds"]


def local_tts(cfg: Any) -> TTSProvider:
    """espeak-ng if installed, else the silent-ish mock voice."""
    espeak = EspeakTTS(voice=cfg.voice, pitch=cfg.pitch)
    return espeak if espeak.available() else MockTTS()


def build_tts(cfg: Any, *, cloud_allowed: Callable[[], bool], api_key: Callable[[], str | None],
              transport: Any = None) -> tuple[TTSProvider, TTSProvider]:
    """cfg: ``TTSConfig``. Returns (primary, local fallback)."""
    provider = getattr(cfg, "provider", "auto")
    if provider == "mock":
        mock = MockTTS()
        return mock, mock
    local = local_tts(cfg)
    if provider == "auto":
        return local, local
    if provider == "espeak":
        if not isinstance(local, EspeakTTS):
            log.warning("tts.provider=espeak but espeak-ng is not installed; using the mock voice")
        return local, local
    if provider == "piper":
        piper = PiperTTS(cfg.piper_model, binary=getattr(cfg, "piper_binary", None))
        if piper.available():
            return piper, local
        log.warning("tts.provider=piper unavailable (%s); using %s", piper.unavailable_reason(), local.name)
        return local, local
    if provider == "openai":
        cloud = OpenAITTS(api_key=api_key, cloud_allowed=cloud_allowed, model=cfg.openai_model,
                          voice=cfg.openai_voice, transport=transport)
        return cloud, local
    log.warning("unknown tts.provider %r; using %s", provider, local.name)
    return local, local
