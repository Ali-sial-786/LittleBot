"""Text-to-speech provider interface and shared helpers.

``synthesize`` is blocking (subprocess / HTTP) and is always called from a
worker thread (``asyncio.to_thread``), never on the event loop.
"""

from __future__ import annotations

import abc
import math
import re
import unicodedata
from typing import Any

import numpy as np

MIN_RATE = 0.5
MAX_RATE = 1.6
MAX_CHARS = 300  # LittleBot speaks short, simple phrases
WORDS_PER_SECOND = 2.5  # gentle speaking pace for a toddler at rate 1.0


class TTSProvider(abc.ABC):
    name: str = "base"
    is_cloud: bool = False

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    @abc.abstractmethod
    def synthesize(self, text: str, rate: float = 1.0) -> tuple[np.ndarray, int]:
        """Return (mono float32 samples, sample_rate). Raises TTSError / CloudNotAllowed."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available(), "cloud": self.is_cloud,
                "detail": "" if self.available() else self.unavailable_reason()}


def clamp_rate(rate: float | None, default: float = 1.0) -> float:
    try:
        r = float(rate if rate is not None else default)
    except (TypeError, ValueError):
        r = default
    if not math.isfinite(r):
        r = default
    return max(MIN_RATE, min(MAX_RATE, r))


_WS = re.compile(r"\s+")


def clean_text(text: Any, max_chars: int = MAX_CHARS) -> str:
    """Printable, emoji-free, single-line text, cut at a word boundary if too long."""
    s = str(text or "")
    kept = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ch in "\n\r\t":
            kept.append(" ")
        elif cat in ("Cc", "Cf", "Cs", "Co", "Cn", "So", "Sk"):
            continue  # control chars, emoji and pictographs (a TTS would read their names)
        else:
            kept.append(ch)
    s = _WS.sub(" ", "".join(kept)).strip()
    if len(s) > max_chars:
        cut = s[:max_chars]
        space = cut.rfind(" ")
        s = (cut[:space] if space > max_chars // 2 else cut).rstrip(" ,;:-") + "."
    return s


def estimate_seconds(text: str, rate: float = 1.0) -> float:
    words = max(1, len(text.split()))
    return max(0.5, words / (WORDS_PER_SECOND * max(MIN_RATE, rate)))
