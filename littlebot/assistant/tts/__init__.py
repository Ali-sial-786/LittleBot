"""The assistant's voice: the configured text-to-speech services in order, as one streaming voice at 48 kHz.

``build_tts_chain(ctx)`` makes a :class:`TTSChain` from ``assistant.tts`` (default cartesia, elevenlabs, openai,
local). For each sentence the chain asks the first voice that can be used now:

* a voice that isn't set up (no key) or not allowed now (cloud off: offline mode or the parent's switch) is
  skipped, with one log line when that changes;
* a voice with a limit on requests at once (``max_concurrent``: Cartesia's free plan allows 2) is never asked
  for more: a sentence waits for its turn (the one before is made far faster than it is heard, so this costs no
  gap), and the wait doesn't count against its first-sound time;
* one that refuses because it is busy (``rate_limit``) is asked again once, a moment later, before another voice
  says the sentence: a different voice in the middle of an answer sounds like a second person in the room;
* one that fails (``SpeechError``) or sends no sound within ``assistant.tts_first_audio_timeout_seconds`` hands
  the same sentence to the next one, and rests for a while (a rejected key: 10 minutes; busy: 15 s; a slow or
  failed answer: 20 s) so the next sentences don't wait for it too. Once any sound of a sentence was handed on, a
  failure is not retried elsewhere (it would repeat words): the ``SpeechError`` goes to the caller;
* "local" (piper / espeak-ng / mock on the robot) is always there, last, whatever ``assistant.tts`` says, and
  isn't given up on early (it has nothing after it).

Every voice's sound is resampled on the fly to ``OUTPUT_RATE`` (48 kHz, the speaker's rate in the live audio
path), chunk by chunk with the phase kept across chunks (``dsp.StreamResampler``), so chunk boundaries never click.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, AsyncIterator, Callable

import numpy as np

from littlebot.assistant.speech import CloudOk, SpeechError, StreamingTTS
from littlebot.assistant.speech_http import cloud_ok_for, is_allowed, key_reader
from littlebot.assistant.tts.cartesia import CartesiaTTS
from littlebot.assistant.tts.elevenlabs import ElevenLabsTTS
from littlebot.assistant.tts.local import LocalTTS
from littlebot.assistant.tts.openai import OpenAITTS
from littlebot.audio.dsp import StreamResampler
from littlebot.audio.tts.base import clean_text

log = logging.getLogger("littlebot.assistant.tts")

OUTPUT_RATE = 48000
MAX_SENTENCE_CHARS = 1000
LAST_VOICE_TIMEOUT_S = 20.0  # the last voice has nothing after it: it gets this long for its first sound
WARM_TIMEOUT_S = 5.0
# after a failure before any sound, a cloud voice is skipped this long (seconds, by SpeechError kind)
REST_S = {"auth": 600.0, "bad_request": 600.0, "rate_limit": 15.0}
REST_DEFAULT_S = 20.0
BUSY_RETRY_S = 0.5  # a voice that said "too many requests at once" is asked again after this
SLOT_WAIT_S = 10.0  # a sentence waits at most this long for its turn with a voice that has a limit


class TTSChain:
    """The voices in order, as one ``StreamingTTS`` at ``OUTPUT_RATE``. ``last_provider``: who spoke last."""

    name = "chain"
    is_cloud = False
    sample_rate = OUTPUT_RATE

    def __init__(self, providers: list[StreamingTTS], *, cloud_ok: CloudOk, first_audio_timeout: float = 2.5,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.providers = list(providers)
        self._cloud_ok = cloud_ok
        self.first_audio_timeout = float(first_audio_timeout)
        self._clock = clock
        self.last_provider: str | None = None
        self.last_error: str | None = None  # "<voice>: <kind>" of the last failure
        self._resting: dict[str, tuple[str, float]] = {}  # voice -> (kind, until)
        self._said_why: dict[str, str] = {}  # voice -> why it was last skipped (logged once per change)
        self._slots: dict[str, tuple[Any, asyncio.Semaphore]] = {}  # voice -> (event loop, its requests at once)

    # ------------------------------------------------------------ which voices can speak now
    def _why_not(self, p: StreamingTTS) -> str | None:
        try:
            why = p.missing()
        except Exception:  # noqa: BLE001 - a voice that can't tell isn't used
            why = "error"
        if why:
            return why
        if getattr(p, "is_cloud", True) and not is_allowed(self._cloud_ok):  # a second lock on the cloud door
            return "cloud_off"
        rest = self._resting.get(p.name)
        if rest is not None:
            if rest[1] > self._clock():
                return f"resting after {rest[0]}"
            del self._resting[p.name]
        return None

    def _usable(self) -> list[StreamingTTS]:
        out = []
        for p in self.providers:
            why = self._why_not(p)
            if why is None:
                if self._said_why.pop(p.name, None) is not None:
                    log.info("assistant tts: %s can be used again", p.name)
                out.append(p)
            elif self._said_why.get(p.name) != why:
                self._said_why[p.name] = why
                log.info("assistant tts: %s skipped (%s)", p.name, why)
        return out

    def missing(self) -> str | None:
        """None when some voice can speak now (always, with the local one), else why the first can't."""
        whys = [self._why_not(p) for p in self.providers]
        return None if any(w is None for w in whys) else (whys[0] if whys else "not_installed")

    # ------------------------------------------------------------ speaking
    async def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        """The sentence as float32 mono chunks at 48 kHz, from the first voice that answers in time."""
        text = clean_text(text, MAX_SENTENCE_CHARS)
        if not text:
            return
        usable = self._usable()
        failure: SpeechError | None = None
        for i, p in enumerate(usable):
            last = i == len(usable) - 1
            timeout = max(self.first_audio_timeout, LAST_VOICE_TIMEOUT_S) if last else self.first_audio_timeout
            for attempt in (1, 2):
                started = False
                try:
                    async with self._slot(p), contextlib.aclosing(self._one(p, text, timeout)) as sound:
                        async for out in sound:  # closed at once when the listener stops (a barge-in)
                            started = True
                            yield out
                    self._resting.pop(p.name, None)
                    return
                except SpeechError as e:
                    if not started and e.kind == "rate_limit" and attempt == 1:
                        log.info("assistant tts: %s is busy; asking again", p.name)
                        await asyncio.sleep(BUSY_RETRY_S)
                        continue
                    failure = self._failed(p, e, started=started, has_next=not last)
                    if started:
                        raise failure from None
                except Exception as e:  # noqa: BLE001 - a voice's bug: the next voice, as for any failure
                    log.debug("assistant tts: %s raised", p.name, exc_info=True)
                    failure = self._failed(p, SpeechError("server_error", type(e).__name__), started=started,
                                           has_next=not last)
                    if started:
                        raise failure from None
                break
        raise failure or SpeechError("not_installed", "no voice")

    async def _one(self, p: StreamingTTS, text: str, timeout: float) -> AsyncIterator[np.ndarray]:
        """One voice's sound for the sentence at 48 kHz; SpeechError if it fails, or has no sound within
        ``timeout``."""
        chunks: Any = None
        try:
            chunks = p.stream(text).__aiter__()
            try:
                async with asyncio.timeout(timeout):
                    first = await _first_sound(chunks)
            except TimeoutError:
                raise SpeechError("timeout", f"no sound within {timeout:g}s") from None
            if first is None:
                raise SpeechError("bad_audio", "no sound")
            self.last_provider = p.name
            resampler = StreamResampler(int(p.sample_rate), OUTPUT_RATE)
            out = resampler.process(first)
            if len(out):
                yield out
            async for chunk in chunks:
                if len(chunk):
                    out = resampler.process(np.asarray(chunk, dtype=np.float32))
                    if len(out):
                        yield out
        finally:
            await _close(chunks)

    @contextlib.asynccontextmanager
    async def _slot(self, p: StreamingTTS) -> AsyncIterator[None]:
        """One of the voice's requests at once, held while it speaks (no limit: nothing to wait for)."""
        limit = int(getattr(p, "max_concurrent", 0) or 0)
        if limit <= 0:
            yield
            return
        loop = asyncio.get_running_loop()
        held = self._slots.get(p.name)
        if held is None or held[0] is not loop:
            held = self._slots[p.name] = (loop, asyncio.Semaphore(limit))
        sem = held[1]
        try:
            async with asyncio.timeout(SLOT_WAIT_S):
                await sem.acquire()
        except TimeoutError:
            raise SpeechError("timeout", f"no turn within {SLOT_WAIT_S:g}s") from None
        try:
            yield
        finally:
            sem.release()

    def _failed(self, p: StreamingTTS, e: SpeechError, *, started: bool, has_next: bool) -> SpeechError:
        self.last_error = f"{p.name}: {e.kind}"
        if started:
            log.warning("assistant tts: %s failed mid-sentence (%s)", p.name, e.kind)
            return e
        if getattr(p, "is_cloud", True):
            self._resting[p.name] = (e.kind, self._clock() + REST_S.get(e.kind, REST_DEFAULT_S))
        log.warning("assistant tts: %s failed (%s)%s", p.name, e.kind, "; trying the next voice" if has_next else "")
        return e

    # ------------------------------------------------------------ lifecycle
    async def warm(self) -> None:
        """Open the first usable voice's connection before the first sentence (best effort)."""
        usable = self._usable()
        if not usable:
            return
        try:
            await asyncio.wait_for(usable[0].warm(), WARM_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            log.debug("assistant tts: warming %s failed", usable[0].name)

    async def aclose(self) -> None:
        for p in self.providers:
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                log.debug("assistant tts: closing %s failed", p.name, exc_info=True)

    def describe(self) -> dict[str, Any]:
        """For the dashboard and the log: the order, who would speak now, who spoke last (never a key)."""
        rows = [{"name": p.name, "cloud": bool(getattr(p, "is_cloud", True)), "missing": self._why_not(p)}
                for p in self.providers]
        return {"sample_rate": OUTPUT_RATE, "order": [r["name"] for r in rows],
                "first": next((r["name"] for r in rows if r["missing"] is None), None),
                "last": self.last_provider, "last_error": self.last_error, "providers": rows}


async def _first_sound(chunks: AsyncIterator[np.ndarray]) -> np.ndarray | None:
    async for chunk in chunks:
        if len(chunk):
            return np.asarray(chunk, dtype=np.float32)
    return None


async def _close(chunks: Any) -> None:
    close = getattr(chunks, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:  # noqa: BLE001
            log.debug("assistant tts: closing a stream failed", exc_info=True)


def build_tts_chain(ctx: Any, *, transport: Any = None, cloud_ok: CloudOk | None = None) -> TTSChain:
    """The voices of ``ctx.config.assistant.tts`` in order, local last. ``transport``: an httpx transport for the
    cloud voices (tests); ``cloud_ok``: a stricter cloud gate than the default (offline mode + privacy switch)."""
    a = ctx.config.assistant
    ok = cloud_ok or cloud_ok_for(ctx)
    providers: list[StreamingTTS] = []
    for name in dict.fromkeys(a.tts):
        if name == "cartesia":
            providers.append(CartesiaTTS(key=key_reader(ctx, "cartesia_api_key"), cloud_ok=ok, model=a.cartesia_model,
                                         voice=a.cartesia_voice, speed=a.cartesia_speed,
                                         emotion=getattr(a, "cartesia_emotion", None), language=a.language,
                                         max_concurrent=getattr(a, "cartesia_concurrency", 1),
                                         transport=transport))
        elif name == "elevenlabs":
            providers.append(ElevenLabsTTS(key=key_reader(ctx, "elevenlabs_api_key"), cloud_ok=ok,
                                           model=a.elevenlabs_model, voice=a.elevenlabs_voice, language=a.language,
                                           transport=transport))
        elif name == "openai":
            providers.append(OpenAITTS(key=key_reader(ctx, "openai_api_key"), cloud_ok=ok, model=a.openai_tts_model,
                                       voice=a.openai_tts_voice, transport=transport))
        elif name == "local":
            providers.append(LocalTTS.from_config(ctx.config.audio.tts))
    if not any(p.name == "local" for p in providers):
        providers.append(LocalTTS.from_config(ctx.config.audio.tts))
    chain = TTSChain(providers, cloud_ok=ok, first_audio_timeout=a.tts_first_audio_timeout_seconds)
    log.info("assistant tts: %s", ", ".join(f"{r['name']}" + (f" ({r['missing']})" if r["missing"] else "")
                                            for r in chain.describe()["providers"]))
    return chain


__all__ = ["OUTPUT_RATE", "TTSChain", "build_tts_chain"]
