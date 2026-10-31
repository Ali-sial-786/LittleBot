"""Stop words while the live voice talks: "stop", "wait", "hold on", "be quiet" ... interrupt it at once.

The barge-in detector (littlebot/chatgpt/live_audio.py, ``BargeInDetector``) needs ``chatgpt.live_barge_in_speech_ms``
of voiced grown-up speech, longer than a quick "stop!"; a parent: "it should look for words like stop even if
they're shorter" (2026-10-29). So while the voice is audible, the echo-cancelled microphone also goes to Vosk (the
wake word's offline model, ``wake.model_path``) in grammar mode: only ``STOP_PHRASES``, the wake word's everyday
``DISTRACTOR_WORDS`` and ``[unk]`` (anything else). Its words so far (the partial result) are checked every 0.1 s,
so a stop word counts as soon as it is said, not when the sentence ends (if its word confidences are high enough
and it is not inside a sentence: see MIN_CONFIDENCE).

``LiveAudio`` only acts on a stop word when the detector saw a sound louder than what is left of the voice's own
echo when it was said (so the voice saying "wait" can't stop itself), not mostly above
``live_barge_in_max_pitch_hz`` (a child, or the voice's own echo leaking through) and not the child by voice
recognition. Then the call goes quiet as for a "stop" in ChatGPT's caption (live.py ``_asked_to_stop``), without
waiting for that caption: for a word this short it often never came. Each frame carries the time it was heard,
so a recogniser that lags on the robot still finds the right moment. Audio stays in memory, in a short queue;
nothing is kept or sent anywhere. Recognition runs in its own thread: the call's audio never waits for it.
Without vosk or its model there are no stop words, and barge-in works as before.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from littlebot.audio import dsp

log = logging.getLogger("littlebot.chatgpt.live")

STOP_PHRASES = ("stop", "stop it", "stop talking", "stop please", "okay stop", "no stop", "wait", "wait a second",
                "wait a minute", "hold on", "hang on", "be quiet", "quiet", "shush", "shh", "enough", "that's enough",
                "pause", "cut it out")
REC_RATE = 16000
BATCH_S = 0.1  # the recogniser gets this much at a time; its words so far are checked after each
QUEUE_FRAMES = 100  # 2 s of 20 ms frames; more waiting (the thread fell behind): the newest are dropped
HIT_FRESH_S = 3.0  # a stop word said this recently (by the time its audio was heard) can still be taken
# The words so far have no confidences: a stop phrase in them forces the result out (``finish``), and counts only
# when each of its words is this sure and at most MAX_LEAD_WORDS of LEAD_WORDS come before it. On the robot
# (2026-10-31, 117 clips in OpenAI voices): every real "stop", "wait", "shush", "pause" was sure (1.0); 13 everyday
# sentences showed one in their words so far ("what's the capital of" as "that's enough"); 10 weren't sure, and the
# other 3 were inside a sentence ("a long story" heard as "a lot stop", "what's the weather" as "what stop").
MIN_CONFIDENCE = 0.6
# A one-word stop phrase, this sure. Said over a long answer, "Stop!" comes out of the echo canceller damaged: on the
# robot (2026-10-31) the news was read on for 38 s after one, as it scored 0.44-0.86 (1.0 alone); the everyday
# sentences' one-word matches all scored 0.00 (the word gone from the final result).
MIN_CONFIDENCE_ONE_WORD = 0.4
LEAD_WORDS = frozenset("hey little lil bot okay ok no oh just please i said um uh alright so and now [unk]".split())
MAX_LEAD_WORDS = 4
_RESET = object()


class StopWords:
    """Listens for ``STOP_PHRASES`` in the microphone frames pushed while the voice talks (``push``); ``take``
    returns the phrase once, when one was heard. ``recognizer_factory()`` -> (recogniser, phrases as word
    tuples), called in the worker thread (loading the model may take ~1 s the first time)."""

    def __init__(self, recognizer_factory: Callable[[], tuple[Any, list[tuple[str, tuple[str, ...]]]] | None], *,
                 rate: int = 48000, now: Callable[[], float] = time.monotonic) -> None:
        self._factory = recognizer_factory
        self.rate = int(rate)
        self._now = now
        self._q: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_FRAMES)
        self._lock = threading.Lock()
        self._hit: tuple[str, float] | None = None
        self._pushing = False
        self._stop = threading.Event()
        self.ready = False
        self.heard = 0
        self._thread = threading.Thread(target=self._work, name="littlebot-stop-words", daemon=True)
        self._thread.start()

    @classmethod
    def from_model(cls, model_path: Path | str | None, *, rate: int = 48000) -> StopWords | None:
        """The stop words with the Vosk model at ``model_path`` (``wake.model_path``), or None without vosk or it."""
        from littlebot.audio.stt.vosk import model_present, resolve_model_path, vosk_installed  # noqa: PLC0415

        path = resolve_model_path(str(model_path) if model_path else None)
        if not vosk_installed() or not model_present(path):
            log.info("live voice: no stop words (%s)", "vosk isn't installed" if not vosk_installed()
                     else f"no speech model at {path}")
            return None
        return cls(lambda: _vosk_recognizer(path), rate=rate)

    # ------------------------------------------------------------ the call's side (event loop)
    def push(self, frame: np.ndarray, at: float | None = None) -> None:
        """20 ms of the echo-cancelled microphone (int16) while the voice is audible, heard ``at``."""
        self._pushing = True
        try:
            self._q.put_nowait((frame, self._now() if at is None else at))
        except queue.Full:
            pass

    def quiet(self) -> None:
        """The voice stopped: what was being recognised is forgotten (no stale partial words later)."""
        if self._pushing:
            self._pushing = False
            try:
                self._q.put_nowait(_RESET)
            except queue.Full:
                pass

    def take(self) -> tuple[str, float] | None:
        """(The stop phrase heard since the last call, when the audio it was found in ended), fresh ones only."""
        with self._lock:
            hit, self._hit = self._hit, None
        if hit is None or self._now() - hit[1] > HIT_FRESH_S:
            return None
        return hit

    def close(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(_RESET)
        except queue.Full:
            pass

    # ------------------------------------------------------------ the worker thread
    def _work(self) -> None:
        try:
            made = self._factory()
        except Exception as e:  # noqa: BLE001 - no stop words; barge-in works as before
            log.warning("live voice: stop words unavailable (%s)", e)
            return
        if made is None:
            return
        rec, phrases = made
        self.ready = True
        batch: list[np.ndarray] = []
        need = round(BATCH_S * self.rate)
        heard_at = 0.0
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is _RESET:
                batch.clear()
                _reset(rec)
                continue
            frame, heard_at = item
            batch.append(frame)
            if sum(len(b) for b in batch) < need:
                continue
            pcm = np.concatenate(batch).astype(np.float32) / 32768.0
            batch.clear()
            if self.rate != REC_RATE:
                pcm = dsp.resample(pcm, self.rate, REC_RATE)
            try:
                result = rec.accept(dsp.to_int16(pcm).tobytes())
                words = (str(result.get("text") or "") if result else str(rec.partial() or "")).split()
                said = match(words, phrases)
                finish = getattr(rec, "finish", None)
                if said is not None and finish is not None:
                    found = result or finish() or {}
                    said = sure(found, phrases)
                    if said is None:
                        log.debug("stop words: not taken: '%s'", found.get("text", ""))
                        _reset(rec)
            except Exception:  # noqa: BLE001 - one bad batch: carry on
                log.debug("stop words: recognition failed", exc_info=True)
                continue
            if said is not None:
                log.debug("stop words: '%s' recognised %d ms after its audio", said,
                          int((self._now() - heard_at) * 1000))
                with self._lock:
                    self._hit = (said, heard_at)
                self.heard += 1
                _reset(rec)


def match(words: list[str], phrases: list[tuple[str, tuple[str, ...]]]) -> str | None:
    """The stop phrase in ``words`` (consecutive), longest first; None when there is none."""
    toks = [w.lower() for w in words]
    for phrase, ph in sorted(phrases, key=lambda p: -len(p[1])):
        n = len(ph)
        if any(tuple(toks[i:i + n]) == ph for i in range(len(toks) - n + 1)):
            return phrase
    return None


def sure(result: dict[str, Any], phrases: list[tuple[str, tuple[str, ...]]]) -> str | None:
    """The stop phrase in a recogniser result when it is what they said (see MIN_CONFIDENCE), else None."""
    words = [w for w in result.get("result") or [] if isinstance(w, dict)]
    toks = [str(w.get("word", "")).lower() for w in words]
    for phrase, ph in sorted(phrases, key=lambda p: -len(p[1])):
        n = len(ph)
        need = MIN_CONFIDENCE_ONE_WORD if n == 1 else MIN_CONFIDENCE
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) != ph or min(float(w.get("conf", 1.0)) for w in words[i:i + n]) < need:
                continue
            if i <= MAX_LEAD_WORDS and all(t in LEAD_WORDS for t in toks[:i]):
                return phrase
    return None


def _reset(rec: Any) -> None:
    try:
        rec.reset()
    except Exception:  # noqa: BLE001
        log.debug("stop words: reset failed", exc_info=True)


def _vosk_recognizer(path: Path) -> tuple[Any, list[tuple[str, tuple[str, ...]]]] | None:
    from littlebot.audio.stt.vosk import load_model  # noqa: PLC0415
    from littlebot.audio.wakeword import VoskKeywordRecognizer, build_grammar  # noqa: PLC0415

    model = load_model(path)  # shared with the wake word (already loaded while it waits for the call to end)
    known = None
    if hasattr(model, "vosk_model_find_word"):
        known = lambda w: model.vosk_model_find_word(w) >= 0  # noqa: E731
    grammar, usable, _dropped = build_grammar(STOP_PHRASES, known=known)
    if not usable:
        log.warning("live voice: no stop words (the speech model knows none of them)")
        return None
    return VoskKeywordRecognizer(model, grammar, REC_RATE), usable
