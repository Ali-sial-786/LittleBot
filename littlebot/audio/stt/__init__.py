"""Speech-to-text providers: mock, Vosk (local, optional) and OpenAI (cloud, gated).

``build_stt`` returns ``(primary, local)``; ``auto`` = Vosk when installed and a
model is configured, else mock. Cloud is never chosen implicitly.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from littlebot.audio.stt.base import STTProvider, STTResult
from littlebot.audio.stt.mock import MockSTT
from littlebot.audio.stt.openai import OpenAISTT
from littlebot.audio.stt.vosk import VoskSTT, vosk_installed

log = logging.getLogger("littlebot.audio.stt")

__all__ = ["STTProvider", "STTResult", "MockSTT", "VoskSTT", "OpenAISTT", "build_stt", "local_stt", "vosk_installed"]


def local_stt(cfg: Any, sample_rate: int = 16000) -> STTProvider:
    vosk = VoskSTT(cfg.vosk_model_path, sample_rate=sample_rate)
    return vosk if vosk.available() else MockSTT()


def build_stt(cfg: Any, *, cloud_allowed: Callable[[], bool], api_key: Callable[[], str | None],
              sample_rate: int = 16000, transport: Any = None) -> tuple[STTProvider, STTProvider]:
    """cfg: ``STTConfig``. Returns (primary, local fallback)."""
    provider = getattr(cfg, "provider", "auto")
    if provider == "mock":
        mock = MockSTT()
        return mock, mock
    local = local_stt(cfg, sample_rate)
    if provider == "auto":
        return local, local
    if provider == "vosk":
        if not isinstance(local, VoskSTT):
            log.warning("stt.provider=vosk unavailable (%s); using mock STT",
                        VoskSTT(cfg.vosk_model_path).unavailable_reason())
        return local, local
    if provider == "openai":
        cloud = OpenAISTT(api_key=api_key, cloud_allowed=cloud_allowed, model=cfg.openai_model,
                          language=cfg.language, transport=transport)
        return cloud, local
    log.warning("unknown stt.provider %r; using %s", provider, local.name)
    return local, local
