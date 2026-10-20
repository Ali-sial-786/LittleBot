"""Mock STT: returns text queued with ``queue_text`` (tests / simulation)."""

from __future__ import annotations

import collections
import threading

import numpy as np

from littlebot.audio.stt.base import STTProvider, STTResult


class MockSTT(STTProvider):
    name = "mock"

    def __init__(self, default_text: str | None = None) -> None:
        self.default_text = default_text
        self._queue: collections.deque[str] = collections.deque(maxlen=32)
        self._lock = threading.Lock()
        self.calls = 0
        self.last_audio_seconds = 0.0

    def queue_text(self, text: str) -> None:
        with self._lock:
            self._queue.append(text)

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> STTResult | None:
        self.calls += 1
        self.last_audio_seconds = len(samples) / float(sample_rate or 1)
        with self._lock:
            text = self._queue.popleft() if self._queue else self.default_text
        return STTResult(text, 1.0) if text else None
