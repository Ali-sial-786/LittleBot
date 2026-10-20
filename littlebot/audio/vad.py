"""Simple energy-based voice activity detection (VAD).

Chunk by chunk: speech *starts* once the RMS stays above the threshold for
``start_seconds``; it *ends* after ``silence_seconds`` below the (lower,
hysteresis) release threshold, or when ``max_record_seconds`` is reached.
A short pre-roll keeps the first syllable. Too-short bursts (claps, bangs)
are discarded. Audio lives only in memory.
"""

from __future__ import annotations

import collections
import enum

import numpy as np

from littlebot.audio import dsp


class VADState(str, enum.Enum):
    WAITING = "waiting"
    SPEECH = "speech"
    DONE = "done"


class EnergyVAD:
    def __init__(self, sample_rate: int = 16000, *, threshold: float = 0.015, silence_seconds: float = 1.2,
                 max_record_seconds: float = 8.0, start_seconds: float = 0.06, min_speech_seconds: float = 0.2,
                 pre_roll_seconds: float = 0.3, release_ratio: float = 0.7, noise_factor: float = 3.0) -> None:
        self.sample_rate = sample_rate
        self.threshold = max(1e-4, float(threshold))
        self.silence_seconds = max(0.1, float(silence_seconds))
        self.max_record_seconds = max(0.5, float(max_record_seconds))
        self.start_seconds = start_seconds
        self.min_speech_seconds = min_speech_seconds
        self.pre_roll_seconds = pre_roll_seconds
        self.release_ratio = release_ratio
        self.noise_factor = noise_factor
        self.reset()

    def reset(self) -> None:
        self.state = VADState.WAITING
        self.reason = ""
        self._pre: collections.deque[np.ndarray] = collections.deque()
        self._pre_samples = 0
        self._chunks: list[np.ndarray] = []
        self._recorded = 0
        self._voiced = 0
        self._above = 0
        self._silence = 0
        self.noise_floor = 0.0
        self.last_rms = 0.0

    @property
    def effective_threshold(self) -> float:
        return max(self.threshold, self.noise_floor * self.noise_factor)

    @property
    def started(self) -> bool:
        return self.state != VADState.WAITING

    @property
    def finished(self) -> bool:
        return self.state == VADState.DONE

    def process(self, chunk: np.ndarray) -> VADState:
        if self.state == VADState.DONE or len(chunk) == 0:
            return self.state
        level = dsp.rms(chunk)
        self.last_rms = level
        sr = self.sample_rate
        n = len(chunk)
        if self.state == VADState.WAITING:
            thr = self.effective_threshold
            if level >= thr:
                self._above += n
            else:
                self._above = 0
                # slowly track the background noise while nobody speaks
                self.noise_floor = level if self.noise_floor == 0 else 0.95 * self.noise_floor + 0.05 * level
            self._pre.append(chunk)
            self._pre_samples += n
            while self._pre and self._pre_samples - len(self._pre[0]) >= self.pre_roll_seconds * sr:
                self._pre_samples -= len(self._pre.popleft())
            if self._above >= self.start_seconds * sr:
                self.state = VADState.SPEECH
                self._chunks = list(self._pre)
                self._recorded = sum(len(c) for c in self._chunks)
                self._voiced = self._above
                self._silence = 0
                self._pre.clear()
                self._pre_samples = 0
            return self.state
        # SPEECH
        self._chunks.append(chunk)
        self._recorded += n
        if level >= self.effective_threshold * self.release_ratio:
            self._voiced += n
            self._silence = 0
        else:
            self._silence += n
        if self._recorded >= self.max_record_seconds * sr:
            self.state, self.reason = VADState.DONE, "max_length"
        elif self._silence >= self.silence_seconds * sr:
            if self._voiced < self.min_speech_seconds * sr:
                floor = self.noise_floor
                self.reset()  # just a bump / clap: keep waiting
                self.noise_floor = floor
            else:
                self.state, self.reason = VADState.DONE, "silence"
        return self.state

    def audio(self) -> np.ndarray:
        """The captured utterance (pre-roll + speech, trailing silence trimmed to 0.3 s)."""
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        x = np.concatenate(self._chunks).astype(np.float32)
        keep_tail = int(0.3 * self.sample_rate)
        if self.reason == "silence" and self._silence > keep_tail:
            x = x[: max(0, len(x) - (self._silence - keep_tail))]
        return x

    def discard(self) -> None:
        """Forget captured audio (privacy: nothing kept longer than needed)."""
        self._chunks = []
        self._pre.clear()
