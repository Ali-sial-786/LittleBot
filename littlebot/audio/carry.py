"""What the person said with the wake word, carried into the live call it starts ("Hey LittleBot, what's the
weather today?" in one breath).

The wake word (littlebot/audio/wakeword.py) fills a :class:`SpeechCarry` with what was said after the wake
phrase, from the moment it was recognised until live voice takes the microphone; the live call
(littlebot/chatgpt/live_audio.py, ``CarryFeed``) sends it before its own microphone audio.

Privacy: the audio lives in memory only, in this object, and is never written anywhere. It is dropped
(``drop``) when the call can't start, doesn't connect or ends, and handed over once (``take``). LittleBot's own
sounds are never added: the wake word leaves out what the microphone hears while LittleBot makes a sound.

Who said it: only while "Recognise family voices" is on (``keep_voice``), the carry also holds the wake phrase
itself and the first ``VOICE_KEEP_SECONDS`` of what followed, for the call to recognise the voice
(littlebot/voices/call.py, ``voice_audio``); that copy is dropped as soon as the call has looked at it
(``forget_voice``) and with the rest (``drop``).
"""

from __future__ import annotations

import collections
import threading
import time

import numpy as np

from littlebot.audio import dsp

QUIET_RATIO = 0.7  # x audio.microphone.vad_threshold: below this a stretch counts as a pause (EnergyVAD's release)
VOICE_KEEP_SECONDS = 5.0  # keep_voice: the wake phrase plus at most this much of what followed


class PreRoll:
    """The sentence the wake word's recogniser is still deciding on (at most ``max_seconds``).

    The recogniser only decides at the end of a sentence, so by the time it reports "hey little bot what
    [unk] the [unk]" the question has been said already: it is taken from here. In memory only, and
    forgotten as soon as the recogniser decides a sentence held no wake phrase (``decided``: only the last
    ``keep_seconds``, a pause, stay), while LittleBot makes a sound and when listening stops (``clear``).
    """

    def __init__(self, sample_rate: int, max_seconds: float = 8.0, keep_seconds: float = 0.3) -> None:
        self.sample_rate = int(sample_rate)
        self._max = int(max_seconds * self.sample_rate)
        self._keep = int(keep_seconds * self.sample_rate)
        self._blocks: collections.deque[tuple[int, np.ndarray]] = collections.deque()
        self._n = 0

    def add(self, at: int, block: np.ndarray) -> None:
        """``block`` as fed to the recogniser; ``at``: its first sample, counted from the recogniser's start."""
        self._blocks.append((int(at), block))
        self._n += len(block)
        self._trim(self._max)

    def decided(self) -> None:
        self._trim(self._keep)

    def clear(self) -> None:
        self._blocks.clear()
        self._n = 0

    def before(self, at: int | None) -> list[np.ndarray]:
        """What was heard before sample ``at`` (nothing when None): the wake phrase, when ``at`` is where it ends."""
        out: list[np.ndarray] = []
        if at is None:
            return out
        for start, block in self._blocks:
            if start >= at:
                break
            out.append(block[: at - start] if start + len(block) > at else block)
        return out

    def since(self, at: int | None) -> list[np.ndarray]:
        """What was heard from sample ``at`` on (everything kept when None)."""
        out: list[np.ndarray] = []
        for start, block in self._blocks:
            if at is not None and start + len(block) <= at:
                continue
            out.append(block[at - start:] if at is not None and start < at else block)
        return out

    @property
    def seconds(self) -> float:
        return self._n / float(self.sample_rate)

    def _trim(self, limit: int) -> None:
        while self._blocks and self._n - len(self._blocks[0][1]) >= limit:
            self._n -= len(self._blocks.popleft()[1])


class SpeechCarry:
    """Mono float32 audio at ``sample_rate``: open (being filled) -> sealed (the call may send it) or dropped."""

    def __init__(self, sample_rate: int, *, phrase: str = "", quiet_level: float = 0.0105,
                 kept_talking: bool = False, heard_at: float | None = None, keep_voice: bool = False) -> None:
        self.sample_rate = int(sample_rate)
        self.phrase = phrase
        self.quiet_level = float(quiet_level)  # RMS (full scale 1.0) below which a stretch is a pause
        self.kept_talking = kept_talking  # the recogniser heard more words after the wake phrase
        # The call's own microphone already listens (it keeps what comes after ``sealed_at``): the wake word hands
        # the microphone over at once, not at their next pause.
        self.taker_listens = False
        self.heard_at = time.monotonic() if heard_at is None else heard_at  # when the wake phrase was recognised
        self.lead_s = 0.0  # seconds of it said before the wake phrase was recognised (the rest of that sentence)
        self.quiet_s = 0.0  # how long it has been quiet at its end (a pause: a good moment to hand over)
        self.state = "open"
        self.why = ""
        self.sealed_at: float | None = None
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._speech = 0
        self.keep_voice = keep_voice  # also keep a copy to recognise who said it (see voice_audio)
        self._voice: list[np.ndarray] = []
        self._voice_n = 0
        self._voice_max = int(VOICE_KEEP_SECONDS * self.sample_rate)

    # ------------------------------------------------------------ filling (the wake word's thread)
    def add(self, chunk: np.ndarray) -> bool:
        """Keep ``chunk`` (False once sealed or dropped)."""
        x = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if not len(x):
            return self.is_open
        with self._lock:
            if self.state != "open":
                return False
            self._chunks.append(x.copy())
            self._samples += len(x)
            if self.keep_voice and self._voice_n < self._voice_max:
                self._voice.append(x[: self._voice_max - self._voice_n].copy())
                self._voice_n += len(self._voice[-1])
            if dsp.rms(x) >= self.quiet_level:
                self._speech += len(x)
                self.quiet_s = 0.0
            else:
                self.quiet_s += len(x) / float(self.sample_rate)
            return True

    def seal(self, why: str = "") -> None:
        """Nothing more is added; the call may now take it."""
        with self._lock:
            if self.state == "open":
                self.state, self.why, self.sealed_at = "sealed", why, time.monotonic()

    def drop(self, why: str = "") -> None:
        """Forget it (the call couldn't start, didn't connect or ended)."""
        with self._lock:
            if self.state != "dropped":
                self.state, self.why = "dropped", why
                if self.sealed_at is None:
                    self.sealed_at = time.monotonic()
            self._chunks = []
            self._voice, self._voice_n, self.keep_voice = [], 0, False

    def keep_wake_phrase(self, blocks: list[np.ndarray]) -> None:
        """The wake phrase itself (said before what is carried), kept only with ``keep_voice``."""
        with self._lock:
            if self.keep_voice and self.state != "dropped" and blocks:
                self._voice.insert(0, np.concatenate([np.asarray(b, dtype=np.float32).reshape(-1) for b in blocks]))

    def voice_audio(self) -> np.ndarray:
        """The wake phrase and the first VOICE_KEEP_SECONDS after it (a copy; empty without ``keep_voice``)."""
        with self._lock:
            parts = list(self._voice)
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def forget_voice(self) -> None:
        with self._lock:
            self._voice, self._voice_n, self.keep_voice = [], 0, False

    def take(self) -> np.ndarray:
        """All of it, once (it is empty afterwards); nothing once dropped."""
        with self._lock:
            chunks, self._chunks = self._chunks, []
            if self.state == "open":
                self.state, self.sealed_at = "sealed", time.monotonic()
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    # ------------------------------------------------------------ views
    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def dropped(self) -> bool:
        return self.state == "dropped"

    @property
    def seconds(self) -> float:
        return self._samples / float(self.sample_rate)

    @property
    def speech_seconds(self) -> float:
        """How much of it was louder than a pause (an estimate, for the log)."""
        return self._speech / float(self.sample_rate)


__all__ = ["PreRoll", "SpeechCarry", "QUIET_RATIO"]
