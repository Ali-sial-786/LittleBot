"""Mock TTS: no real voice, but realistic timing so the face can animate.

``style="babble"`` (default) produces very quiet, soft syllable blips whose
envelope drives the mouth animation; ``style="silence"`` produces silence of
the same duration.
"""

from __future__ import annotations

import math
import zlib

import numpy as np

from littlebot.audio import dsp
from littlebot.audio.tts.base import TTSProvider, estimate_seconds


class MockTTS(TTSProvider):
    name = "mock"

    def __init__(self, *, style: str = "babble", sample_rate: int = 22050, level: float = 0.05) -> None:
        if style not in ("babble", "silence"):
            raise ValueError("style must be 'babble' or 'silence'")
        self.style = style
        self.sample_rate = sample_rate
        self.level = level
        self.calls: list[tuple[str, float]] = []

    def synthesize(self, text: str, rate: float = 1.0) -> tuple[np.ndarray, int]:
        self.calls.append((text, rate))
        sr = self.sample_rate
        dur = estimate_seconds(text, rate)
        n = int(dur * sr)
        if self.style == "silence":
            return np.zeros(n, dtype=np.float32), sr
        rng = np.random.default_rng(zlib.crc32(text.encode("utf-8")))
        out = np.zeros(n, dtype=np.float64)
        words = max(1, len(text.split()))
        word_len = dur / words
        pos = 0.0
        for _ in range(words):
            syllables = int(rng.integers(1, 3))
            syl_len = word_len * 0.8 / syllables
            for _s in range(syllables):
                k = int(syl_len * 0.75 * sr)
                start = int(pos * sr)
                if k <= 8 or start + k > n:
                    break
                t = np.arange(k) / sr
                f0 = float(rng.uniform(240, 380))
                f = f0 * (1 + 0.06 * np.sin(math.pi * t / t[-1]))
                ph = 2 * math.pi * np.cumsum(f) / sr
                blip = (np.sin(ph) + 0.25 * np.sin(2 * ph)) * np.sin(math.pi * t / t[-1]) ** 2
                out[start:start + k] += blip
                pos += syl_len
            pos += word_len * 0.2
        x = dsp.normalize_peak(out.astype(np.float32), self.level) if np.any(out) else out.astype(np.float32)
        return x.astype(np.float32), sr
