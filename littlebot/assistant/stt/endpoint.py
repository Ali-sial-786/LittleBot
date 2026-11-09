"""LittleBot's own end-of-turn, for speech-to-text that only transcribes (OpenAI) or doesn't know when a turn ends
(Vosk): :class:`Endpointer` is the ``STTStream`` the session talks to, :class:`EndpointedSTT` the provider around it.

The microphone audio (16 kHz) is looked at in 20 ms frames:

* speech starts when at least 100 ms of the last 200 ms were louder than the threshold
  (``audio.microphone.vad_threshold``, 0.015 RMS on the robot's echo-cancelled ReSpeaker, or 3x the room's noise
  floor when that is louder; the floor follows the quiet frames, as littlebot/audio/vad.py's does):
  ``start_of_turn``, and the turn's audio is kept, in memory only, from 300 ms before;
* while they talk, a frame quieter than 0.7x that threshold (the wake word's QUIET_RATIO) counts as a pause. The
  turn ends after ``assistant.end_of_turn_ms`` of pause: 0.7x that when the words so far (from a provider that
  gives them as they are said: Vosk) sound like a whole sentence, 1.8x (at most 2 s) when they stop on "and",
  "um", "the", "because" ...; and after ``assistant.max_turn_seconds`` whatever happens (or ``finish()``);
* then the turn is transcribed in the background (listening goes on meanwhile) and ``end_of_turn`` comes with
  the words. No words, or less than 250 ms of voice (a cough, a door): an empty ``end_of_turn``, and listening
  again.

Timing comes only from the audio fed (samples counted, no clock), so a test's synthetic audio gives the same
result every time. The turn's audio is dropped as soon as it was transcribed (or on ``reset`` / ``aclose``).
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
from typing import Any

import numpy as np

from littlebot.assistant.speech import STT_RATE, OnEvent, SpeechError, STTEvent
from littlebot.assistant.stt.base import BatchSTT, TurnDecoder, emit
from littlebot.audio import dsp

log = logging.getLogger("littlebot.assistant.stt.endpoint")

FRAME_MS = 20
START_WINDOW_MS = 200  # speech starts when START_VOICED_MS of the last START_WINDOW_MS were loud
START_VOICED_MS = 100
PRE_ROLL_MS = 300  # kept from before the start (the first syllable)
MIN_VOICE_MS = 250  # less voice than this in a "turn": a bump, not words
KEEP_TAIL_MS = 300  # of the final pause, sent with the turn
DECODE_EVERY_MS = 100  # a decoder (Vosk) gets the audio in pieces this long
RELEASE_RATIO = 0.7  # x the start threshold: quieter is a pause (littlebot/audio/carry.py QUIET_RATIO)
NOISE_FACTOR = 3.0  # the start threshold is at least this x the noise floor
NOISE_FLOOR_MAX = 0.03  # a very loud room doesn't push the threshold past 3x this
COMPLETE_FACTOR = 0.7
OPEN_FACTOR = 1.8
OPEN_MAX_MS = 2000

# The words so far ending on one of these: more is coming (a filler, a conjunction, an article, a preposition).
OPEN_WORDS = frozenset({
    "and", "but", "um", "uh", "so", "the", "to", "of", "because", "like",
    "or", "a", "an", "er", "erm", "uhm", "umm", "hmm", "mm", "with", "for", "if", "then", "than", "my", "your",
    "our", "is", "are", "was", "were",
})
# ... on one of these: not a whole sentence yet, nor clearly more coming (the plain pause).
NEUTRAL_WORDS = frozenset({
    "i", "you", "we", "they", "he", "she", "what", "what's", "where", "when", "why", "how", "who", "which",
    "can", "could", "would", "will", "should", "do", "does", "did", "have", "has", "let", "let's",
})
_WORDS = re.compile(r"[a-z']+")


def pause_needed_ms(words_so_far: str, end_of_turn_ms: float) -> float:
    """How long a pause ends the turn, after ``words_so_far`` ("" when the provider gives none)."""
    base = float(end_of_turn_ms)
    text = words_so_far.strip()
    words = _WORDS.findall(text.lower())
    if not words:
        return base
    if words[-1] in OPEN_WORDS or text.endswith((",", "-", "...")):
        return max(base, min(base * OPEN_FACTOR, OPEN_MAX_MS))
    if text.endswith((".", "?", "!")) or (len(words) >= 3 and words[-1] not in NEUTRAL_WORDS):
        return base * COMPLETE_FACTOR
    return base


class Endpointer:
    """An ``STTStream``: a batch ``provider`` with LittleBot's end-of-turn (see the module docstring)."""

    def __init__(self, provider: BatchSTT, on_event: OnEvent, *, end_of_turn_ms: float = 650.0,
                 max_turn_seconds: float = 30.0, threshold: float = 0.015, sample_rate: int = STT_RATE) -> None:
        self.provider = provider
        self._on_event = on_event
        self.end_of_turn_ms = float(end_of_turn_ms)
        self.max_turn_seconds = float(max_turn_seconds)
        self.threshold = max(1e-4, float(threshold))
        self.sample_rate = int(sample_rate)
        self._frame = self.sample_rate * FRAME_MS // 1000
        self._start_frames = START_VOICED_MS // FRAME_MS
        self._recent: collections.deque[bool] = collections.deque(maxlen=START_WINDOW_MS // FRAME_MS)
        self._pre: collections.deque[np.ndarray] = collections.deque(maxlen=PRE_ROLL_MS // FRAME_MS)
        self._buf = np.zeros(0, dtype=np.float32)
        self.noise_floor = 0.0
        self.state = "waiting"  # | "speech"
        self.last_level = 0.0
        self.partial = ""  # the decoder's words so far in this turn
        self._turn: list[np.ndarray] = []
        self._turn_n = 0
        self._voiced = 0
        self._quiet = 0
        self._turn_threshold = self.threshold
        self._decoder: TurnDecoder | None = None
        self._pending: list[np.ndarray] = []  # audio not given to the decoder yet
        self._pending_n = 0
        self._task: asyncio.Task[None] | None = None  # the last turn's transcription
        self._gen = 0  # +1 on reset / aclose: a transcription of an earlier generation is dropped
        self._closed = False

    @property
    def start_threshold(self) -> float:
        return max(self.threshold, min(self.noise_floor, NOISE_FLOOR_MAX) * NOISE_FACTOR)

    # ------------------------------------------------------------ STTStream
    async def feed(self, samples: np.ndarray) -> None:
        if self._closed:
            return
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        buf = np.concatenate([self._buf, x]) if len(self._buf) else x
        f = self._frame
        whole = len(buf) // f * f
        for i in range(0, whole, f):
            await self._frame_in(buf[i:i + f])
            if self._closed:
                return
        self._buf = buf[whole:].copy()

    async def finish(self) -> None:
        if self.state == "speech":
            await self._end_turn("finish")
        task = self._task
        if task is not None:
            await asyncio.wait([task])

    async def reset(self) -> None:
        self._gen += 1
        self._cancel()
        await self._back_to_waiting()
        self._buf = np.zeros(0, dtype=np.float32)

    async def aclose(self) -> None:
        self._closed = True
        await self.reset()

    async def idle(self) -> None:
        """Wait for the last turn's transcription (tests)."""
        task = self._task
        if task is not None:
            await asyncio.wait([task])

    # ------------------------------------------------------------ turns
    async def _frame_in(self, frame: np.ndarray) -> None:
        level = dsp.rms(frame)
        self.last_level = level
        n = len(frame)
        if self.state == "waiting":
            loud = level >= self.start_threshold
            if not loud:  # the room's background, while nobody talks
                self.noise_floor = level if self.noise_floor == 0 else 0.95 * self.noise_floor + 0.05 * level
            self._recent.append(loud)
            self._pre.append(frame)
            if loud and sum(self._recent) >= self._start_frames:
                await self._start_turn()
            return
        self._turn.append(frame)
        self._turn_n += n
        if level >= self._turn_threshold * RELEASE_RATIO:
            self._voiced += n
            self._quiet = 0
        else:
            self._quiet += n
        if self._decoder is not None:
            self._pending.append(frame)
            self._pending_n += n
            if self._pending_n >= DECODE_EVERY_MS * self.sample_rate // 1000:
                await self._decode()
        if self._turn_n >= self.max_turn_seconds * self.sample_rate:
            await self._end_turn("max_length")
        elif self._quiet >= pause_needed_ms(self.partial, self.end_of_turn_ms) * self.sample_rate / 1000.0:
            await self._end_turn("pause")

    async def _start_turn(self) -> None:
        self.state = "speech"
        self._turn_threshold = self.start_threshold
        self._turn = list(self._pre)
        self._turn_n = sum(len(c) for c in self._turn)
        self._voiced = sum(self._recent) * self._frame
        self._quiet = 0
        self.partial = ""
        self._pre.clear()
        self._recent.clear()
        make = getattr(self.provider, "decoder", None)
        if make is not None:
            try:
                self._decoder = make()
            except SpeechError as e:
                log.info("assistant stt: %s can't follow the words (%s); transcribing at the end",
                         self.provider.name, e.kind)
                self._decoder = None
            self._pending, self._pending_n = list(self._turn), self._turn_n
        await emit(self._on_event, STTEvent("start_of_turn"))

    async def _decode(self) -> None:
        decoder = self._decoder
        if decoder is None or not self._pending:
            return
        pcm = np.concatenate(self._pending)
        self._pending, self._pending_n = [], 0
        try:
            words = (await decoder.accept(pcm) or "").strip()
        except SpeechError as e:
            log.warning("assistant stt: %s failed while following the words (%s)", self.provider.name, e.kind)
            self._decoder = None
            await _close(decoder)
            return
        if words and words != self.partial and self.state == "speech":
            self.partial = words
            await emit(self._on_event, STTEvent("partial", text=words))

    async def _end_turn(self, why: str) -> None:
        voiced, ms = self._voiced, int(self._turn_n * 1000 / self.sample_rate)
        audio = np.concatenate(self._turn) if self._turn else np.zeros(0, dtype=np.float32)
        tail = KEEP_TAIL_MS * self.sample_rate // 1000
        if why == "pause" and self._quiet > tail:
            audio = audio[: max(0, len(audio) - (self._quiet - tail))]
        decoder, self._decoder = self._decoder, None
        rest = np.concatenate(self._pending) if decoder is not None and self._pending else None
        self._pending, self._pending_n = [], 0
        self._turn, self._turn_n, self.state, self.partial = [], 0, "waiting", ""
        if voiced < MIN_VOICE_MS * self.sample_rate // 1000:
            log.debug("assistant stt: %d ms of sound with little voice: not a turn", ms)
            await _close(decoder)
            await emit(self._on_event, STTEvent("end_of_turn", text=""))  # the session listens again at once
            return
        log.debug("assistant stt: turn ended (%s) after %d ms", why, ms)
        self._task = asyncio.create_task(self._transcribe(self._task, self._gen, audio, decoder, rest))

    async def _transcribe(self, before: asyncio.Task[None] | None, gen: int, audio: np.ndarray,
                          decoder: TurnDecoder | None, rest: np.ndarray | None) -> None:
        if before is not None:
            await asyncio.wait([before])  # the turns' words go out in order
        try:
            if decoder is not None:
                if rest is not None and len(rest):
                    await decoder.accept(rest)
                text = await decoder.final()
            else:
                text = await self.provider.transcribe(audio)
        except SpeechError as e:
            log.warning("assistant stt: %s failed (%s)", self.provider.name, e.kind)
            if gen == self._gen:
                await emit(self._on_event, STTEvent("error", detail=e.kind))
            return
        except Exception as e:  # noqa: BLE001 - a provider's bug: reported like a failure
            log.warning("assistant stt: %s failed (%s)", self.provider.name, type(e).__name__)
            if gen == self._gen:
                await emit(self._on_event, STTEvent("error", detail="server_error"))
            return
        finally:
            del audio
            await _close(decoder)
        text = (text or "").strip()
        if gen == self._gen and not self._closed:  # no words: an empty end, so the session stops waiting for them
            await emit(self._on_event, STTEvent("end_of_turn", text=text))

    async def _back_to_waiting(self) -> None:
        decoder, self._decoder = self._decoder, None
        await _close(decoder)
        self._turn, self._turn_n, self._pending, self._pending_n = [], 0, [], 0
        self._pre.clear()
        self._recent.clear()
        self.state, self.partial, self._voiced, self._quiet = "waiting", "", 0, 0

    def _cancel(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():  # (a handler may reset)
            task.cancel()


async def _close(decoder: Any) -> None:
    if decoder is None:
        return
    try:
        await decoder.aclose()
    except Exception:  # noqa: BLE001
        log.debug("assistant stt: closing a decoder failed", exc_info=True)


class EndpointedSTT:
    """A batch provider as an ``STTProvider`` whose streams find the end of a turn themselves (``Endpointer``).
    ``streaming`` stays False: the end of the turn is LittleBot's guess, not the service's."""

    streaming = False

    def __init__(self, inner: BatchSTT, *, end_of_turn_ms: float = 650.0, max_turn_seconds: float = 30.0,
                 threshold: float = 0.015) -> None:
        self.inner = inner
        self.name = inner.name
        self.is_cloud = bool(getattr(inner, "is_cloud", False))
        self.end_of_turn_ms = end_of_turn_ms
        self.max_turn_seconds = max_turn_seconds
        self.threshold = threshold
        self._warming: asyncio.Task[None] | None = None

    def missing(self) -> str | None:
        return self.inner.missing()

    async def open(self, on_event: OnEvent) -> Endpointer:
        why = self.missing()
        if why:
            raise SpeechError(why)
        warm = getattr(self.inner, "warm", None)
        if warm is not None:
            if self.is_cloud:  # the connection opens while they start talking (best effort, never waited for)
                self._warming = asyncio.create_task(warm())
            else:
                await warm()  # Vosk: the model is loaded now (or fails now), not at the first word
        return Endpointer(self.inner, on_event, end_of_turn_ms=self.end_of_turn_ms,
                          max_turn_seconds=self.max_turn_seconds, threshold=self.threshold)

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


__all__ = ["EndpointedSTT", "Endpointer", "pause_needed_ms"]
