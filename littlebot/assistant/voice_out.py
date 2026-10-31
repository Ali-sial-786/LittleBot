"""The assistant's mouth: sentences in, text-to-speech out, in order, as soon as each one is ready.

``say(sentence)`` queues one; the worker asks the TTS for it at once (and for the next one while it plays, so there
is no gap between sentences) and hands the audio to the output (``chatgpt.live_audio.LiveAudio.play_pcm``) a little
ahead of the speaker (``AHEAD_S``): never more, so a barge-in or "stop" cuts it within a fraction of a second. When
a sentence is about to be heard, ``on_start(sentence)`` runs (the face's expression for it, the body language's
nods). ``cancel()`` stops everything at once; ``done()`` waits until the last word has been heard.

``gate`` (an early answer, started before their turn really ended): the voice is made at once, but nothing is handed
to the speaker until the gate is set, so the first words are ready the moment the answer is confirmed.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import Any, Callable

import numpy as np

from littlebot.assistant.sentences import Sentence
from littlebot.assistant.speech import MOOD

log = logging.getLogger("littlebot.assistant.voice")

AHEAD_S = 0.5  # audio handed to the speaker ahead of what is being heard
# The quiet start of each sentence is cut: Cartesia begins every sentence with 0.2-0.9 s of near-silence (measured on
# the robot, 2026-10-31), heard as a pause before the first word and between sentences.
VOICE_LEVEL = 0.02  # louder than this is the voice
LEAD_FIRST_S = 0.03  # kept before the first word of an answer
LEAD_NEXT_S = 0.12  # kept before the first word of each later sentence (a natural pause after the one before)
MAX_CUT_S = 1.5  # never more than this is cut from a sentence
EXCITED = frozenset({"excited", "celebrating", "surprised"})  # the moods whose "!" are kept
PACE_STEP_S = 0.02
PREFETCH = 2  # sentences being synthesised at once (the one playing and the next)
CHUNK_STALL_S = 15.0  # no audio for a sentence for this long: it is skipped (a voice that hangs never hangs LittleBot)
DRAIN_MAX_S = 10.0  # done(): the last audio handed over is heard within this (it is at most AHEAD_S + the echo tail)


def spoken(s: Sentence) -> str:
    """The words as the voice gets them: an exclamation mark makes most voices louder and pushier, so it stays
    only where the moment really is exciting."""
    return s.text if s.expression in EXCITED else s.text.replace("!", ".")


class VoiceOut:
    def __init__(self, tts: Any, output: Any, *, on_start: Callable[[Sentence], Any] | None = None,
                 now: Callable[[], float] = time.monotonic, gate: asyncio.Event | None = None) -> None:
        self.tts = tts
        self.output = output
        self.on_start = on_start
        self._now = now
        self._gate = gate
        self._queue: collections.deque[Sentence] = collections.deque()
        self._wake = asyncio.Event()
        self._closed = False
        self._cancelled = False
        self._worker: asyncio.Task[Any] | None = None
        self._fetching: collections.deque[tuple[Sentence, asyncio.Queue[Any], asyncio.Task[Any]]] = collections.deque()
        self._timers: list[asyncio.TimerHandle] = []
        self.heard: list[str] = []  # sentences that started playing (what they actually heard)
        self.first_audio_at: float | None = None
        self.cut_s = 0.0  # quiet cut from the sentences' starts (the turn's log line says how much)
        self.failures = 0
        self.providers: list[str] = []

    # ------------------------------------------------------------ the session's side
    def say(self, sentence: Sentence) -> None:
        if self._cancelled or self._closed:
            return
        self._queue.append(sentence)
        self._wake.set()
        if self._worker is None:
            self._worker = asyncio.ensure_future(self._run())

    def close(self) -> None:
        """No more sentences for this answer."""
        self._closed = True
        self._wake.set()

    def cancel(self) -> None:
        """Stop now: nothing more is synthesised or queued (the output's own queue is the caller's to clear)."""
        self._cancelled = True
        self._queue.clear()
        self._wake.set()
        for t in self._timers:
            t.cancel()
        self._timers.clear()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    @property
    def busy(self) -> bool:
        return bool(self._queue) or (self._worker is not None and not self._worker.done())

    async def done(self) -> bool:
        """Wait until every sentence was played and heard (False: cancelled). A story takes as long as it takes:
        only a sentence whose voice stalls is given up (``CHUNK_STALL_S``). The caller's own cancellation is never
        swallowed here."""
        worker = self._worker
        if worker is not None:
            await asyncio.wait({worker})  # raises CancelledError if *we* are cancelled; the worker keeps its own fate
            if worker.cancelled():
                return False
        deadline = self._now() + DRAIN_MAX_S
        while not self._cancelled and self._now() < deadline:
            if float(getattr(self.output, "queued_s", 0.0) or 0.0) <= 0.0 and not getattr(self.output, "speaking",
                                                                                            False):
                return True
            await asyncio.sleep(PACE_STEP_S * 2)
        return not self._cancelled

    # ------------------------------------------------------------ the worker
    def _top_up(self) -> None:
        """Ask the voice for the next sentences while one plays (no gap between sentences)."""
        while self._queue and len(self._fetching) < PREFETCH:
            s = self._queue.popleft()
            q: asyncio.Queue[Any] = asyncio.Queue()
            self._fetching.append((s, q, asyncio.ensure_future(self._fetch(s, q))))

    async def _run(self) -> None:
        try:
            while True:
                self._top_up()
                if not self._fetching:
                    if self._closed or self._cancelled:
                        return
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                s, q, task = self._fetching.popleft()
                try:
                    await self._play(s, q)
                finally:
                    if not task.done():
                        task.cancel()
        finally:
            for _, _, task in self._fetching:
                task.cancel()
            self._fetching.clear()

    async def _fetch(self, s: Sentence, q: asyncio.Queue[Any]) -> None:
        MOOD.set(s.expression)  # this task's own context: the voice sees this sentence's mood
        try:
            async for chunk in self.tts.stream(spoken(s)):
                await q.put(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - every TTS failed: this sentence is skipped
            self.failures += 1
            log.warning("assistant: no voice for a sentence (%s)", getattr(e, "kind", type(e).__name__))
        finally:
            q.put_nowait(None)

    async def _play(self, s: Sentence, q: asyncio.Queue[Any]) -> None:
        rate = int(getattr(self.tts, "sample_rate", 48000) or 48000)
        started = False
        lead = int(rate * (LEAD_FIRST_S if self.first_audio_at is None else LEAD_NEXT_S))
        quiet: np.ndarray | None = np.zeros(0, dtype=np.float32)  # the end of its quiet start (None: it speaks)
        cut = 0
        while True:
            self._top_up()  # the next sentence is being made while this one plays
            try:
                chunk = await asyncio.wait_for(q.get(), CHUNK_STALL_S)
            except asyncio.TimeoutError:
                self.failures += 1
                log.warning("assistant: the voice stalled for %.0f s in a sentence; skipping the rest of it",
                            CHUNK_STALL_S)
                return
            if self._cancelled:
                return
            if chunk is None:
                if quiet is not None and len(quiet):  # never louder than a whisper: what was kept of it
                    self.cut_s += cut / rate
                    await self._out(s, quiet, rate, started)
                return
            pcm = np.asarray(chunk, dtype=np.float32).reshape(-1)
            if not len(pcm):
                continue
            if quiet is not None:
                loud = np.flatnonzero(np.abs(pcm) > VOICE_LEVEL)
                both = np.concatenate([quiet, pcm])
                if not len(loud) and cut + len(pcm) <= MAX_CUT_S * rate:
                    keep = min(len(both), lead)
                    cut += len(both) - keep
                    quiet = both[len(both) - keep:]
                    continue
                start = max(0, len(quiet) + (int(loud[0]) if len(loud) else 0) - lead)
                cut += start
                self.cut_s += cut / rate
                pcm, quiet = both[start:], None
            if not await self._out(s, pcm, rate, started):
                return
            started = True

    async def _out(self, s: Sentence, pcm: np.ndarray, rate: int, started: bool) -> bool:
        """Hand ``pcm`` to the speaker once it has room (paced: never more than AHEAD_S waiting) and the gate is open.
        False: cancelled."""
        if self._gate is not None and not self._gate.is_set():
            await self._gate.wait()  # (cancel() cancels this wait with the worker)
        while float(getattr(self.output, "queued_s", 0.0) or 0.0) > AHEAD_S:
            await asyncio.sleep(PACE_STEP_S)
            if self._cancelled:
                return False
        if not started:
            self._started(s, float(getattr(self.output, "queued_s", 0.0) or 0.0))
        self.output.play_pcm(pcm, rate)
        return True

    def _started(self, s: Sentence, delay: float) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = self._now()
        provider = getattr(self.tts, "last_provider", None)
        if provider and provider not in self.providers:
            self.providers.append(str(provider))

        def heard() -> None:
            self.heard.append(s.text)
            if self.on_start is not None:
                try:
                    self.on_start(s)
                except Exception:  # noqa: BLE001
                    log.debug("assistant: on_start failed", exc_info=True)

        if delay <= 0.01:
            heard()
        else:
            with contextlib.suppress(RuntimeError):
                self._timers.append(asyncio.get_running_loop().call_later(delay, heard))


__all__ = ["AHEAD_S", "VoiceOut"]
