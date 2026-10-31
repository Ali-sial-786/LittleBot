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

The wake phrase for speech-to-text (``keep_phrase`` / ``take_phrase``, at most ``PHRASE_KEEP_SECONDS``): the
assistant sends it before the rest, so speech-to-text hears "hey little bot, who was Kublai Khan" whole instead of
a sentence that starts in the middle of "bot" (heard as "not who was Kublai Khan"), and takes the wake phrase off
the words. Like the rest: memory only, handed over once, dropped with it. GPT-Live never asks for it.
"""

from __future__ import annotations

import collections
import threading
import time

import numpy as np

from littlebot.audio import dsp

QUIET_RATIO = 0.7  # x audio.microphone.vad_threshold: below this a stretch counts as a pause (EnergyVAD's release)
VOICE_KEEP_SECONDS = 5.0  # keep_voice: the wake phrase plus at most this much of what followed
# keep_phrase: the end of what was heard before the carry ("...bot"). Speech-to-text works through what it is sent
# at about real time: 2.5 s of it delayed the end of their turn by ~2 s (the robot, 2026-10-31).
PHRASE_KEEP_SECONDS = 0.6
# The carry starts where the recogniser says the wake phrase ended, often inside "bot": on the robot (2026-10-31)
# the echo-cancelled microphone stayed louder than a pause for ~0.3 s more (the rest of the word and its tail), and
# a bare "hey little bot" was taken for going on talking (no earcon). Speech in that stretch isn't ``talk_seconds``.
TAIL_SECONDS = 0.3


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
        # The call took over while the wake word still decides on its earcon (wakeword EARLY_START): the call's
        # microphone tells it how loud they are (``heard``) until it has decided.
        self.levels_wanted = False
        self.state = "open"
        self.why = ""
        self.sealed_at: float | None = None
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._speech = 0
        self._pos = 0  # samples since the wake phrase ended, added or ``heard``
        self._talk = 0  # loud samples after TAIL_SECONDS
        self._tail = int(TAIL_SECONDS * self.sample_rate)
        self.keep_voice = keep_voice  # also keep a copy to recognise who said it (see voice_audio)
        self._voice: list[np.ndarray] = []
        self._voice_n = 0
        self._voice_max = int(VOICE_KEEP_SECONDS * self.sample_rate)
        self._phrase: np.ndarray | None = None  # the wake phrase, for speech-to-text (keep_phrase)

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
            self._level(dsp.rms(x), len(x))
            return True

    def _level(self, level: float, n: int) -> None:
        """``n`` samples this loud were heard (under the lock)."""
        if level >= self.quiet_level:
            self._speech += n
            if self._pos >= self._tail:
                self._talk += n
            self.quiet_s = 0.0
        else:
            self.quiet_s += n / float(self.sample_rate)
        self._pos += n

    def heard(self, level: float, seconds: float) -> None:
        """How loud the call's microphone was for ``seconds`` once it took over (sealed): only that, no audio,
        while ``levels_wanted`` (the wake word deciding whether they go on talking)."""
        with self._lock:
            if self.state != "sealed" or seconds <= 0:
                return
            self._level(level, int(seconds * self.sample_rate))

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
            self._phrase = None

    def keep_phrase(self, blocks: list[np.ndarray]) -> None:
        """What was heard just before the carry (the wake phrase: its last PHRASE_KEEP_SECONDS), for speech-to-text."""
        with self._lock:
            if self.state != "dropped" and blocks:
                x = np.concatenate([np.asarray(b, dtype=np.float32).reshape(-1) for b in blocks])
                self._phrase = x[-int(PHRASE_KEEP_SECONDS * self.sample_rate):].copy()

    def take_phrase(self) -> np.ndarray:
        """The wake phrase, once (empty afterwards, or without one)."""
        with self._lock:
            x, self._phrase = self._phrase, None
        return x if x is not None else np.zeros(0, dtype=np.float32)

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

    @property
    def talk_seconds(self) -> float:
        """Speech after the wake phrase's own tail (TAIL_SECONDS): are they going on talking?"""
        return self._talk / float(self.sample_rate)


__all__ = ["PreRoll", "SpeechCarry", "QUIET_RATIO"]
