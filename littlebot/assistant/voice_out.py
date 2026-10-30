"""The assistant's mouth: sentences in, text-to-speech out, in order, as soon as each one is ready.

``say(sentence)`` queues one; the worker asks the TTS for it at once (and for the next one while it plays, so there
is no gap between sentences) and hands the audio to the output (``chatgpt.live_audio.LiveAudio.play_pcm``) a little
ahead of the speaker (``AHEAD_S``): never more, so a barge-in or "stop" cuts it within a fraction of a second. When
a sentence is about to be heard, ``on_start(sentence)`` runs (the face's expression for it, the body language's
nods). ``cancel()`` stops everything at once; ``done()`` waits until the last word has been heard.
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

log = logging.getLogger("littlebot.assistant.voice")

AHEAD_S = 0.5  # audio handed to the speaker ahead of what is being heard
PACE_STEP_S = 0.02
PREFETCH = 2  # sentences being synthesised at once (the one playing and the next)


class VoiceOut:
    def __init__(self, tts: Any, output: Any, *, on_start: Callable[[Sentence], Any] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.tts = tts
        self.output = output
        self.on_start = on_start
        self._now = now
        self._queue: collections.deque[Sentence] = collections.deque()
        self._wake = asyncio.Event()
        self._closed = False
        self._cancelled = False
        self._worker: asyncio.Task[Any] | None = None
        self._timers: list[asyncio.TimerHandle] = []
        self.heard: list[str] = []  # sentences that started playing (what they actually heard)
        self.first_audio_at: float | None = None
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

    async def done(self, timeout: float = 120.0) -> bool:
        """Wait until every sentence was played and heard (False: cancelled or timed out)."""
        deadline = self._now() + timeout
        if self._worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._worker), max(0.0, deadline - self._now()))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return False
            except Exception:  # noqa: BLE001 - logged by the worker
                return False
        while not self._cancelled and self._now() < deadline:
            if float(getattr(self.output, "queued_s", 0.0) or 0.0) <= 0.0 and not getattr(self.output, "speaking",
                                                                                            False):
                return True
            await asyncio.sleep(PACE_STEP_S * 2)
        return not self._cancelled

    # ------------------------------------------------------------ the worker
    async def _run(self) -> None:
        fetching: collections.deque[tuple[Sentence, asyncio.Queue[Any], asyncio.Task[Any]]] = collections.deque()
        try:
            while True:
                while self._queue and len(fetching) < PREFETCH:
                    s = self._queue.popleft()
                    q: asyncio.Queue[Any] = asyncio.Queue()
                    fetching.append((s, q, asyncio.ensure_future(self._fetch(s, q))))
                if not fetching:
                    if self._closed or self._cancelled:
                        return
                    self._wake.clear()
                    await self._wake.wait()
                    continue
                s, q, task = fetching.popleft()
                try:
                    await self._play(s, q)
                finally:
                    if not task.done():
                        task.cancel()
        finally:
            for _, _, task in fetching:
                task.cancel()

    async def _fetch(self, s: Sentence, q: asyncio.Queue[Any]) -> None:
        try:
            async for chunk in self.tts.stream(s.text):
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
        while True:
            chunk = await q.get()
            if chunk is None or self._cancelled:
                return
            pcm = np.asarray(chunk, dtype=np.float32).reshape(-1)
            if not len(pcm):
                continue
            while float(getattr(self.output, "queued_s", 0.0) or 0.0) > AHEAD_S:
                await asyncio.sleep(PACE_STEP_S)
                if self._cancelled:
                    return
            if not started:
                started = True
                self._started(s, float(getattr(self.output, "queued_s", 0.0) or 0.0))
            self.output.play_pcm(pcm, rate)

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
