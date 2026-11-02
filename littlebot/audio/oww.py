"""LittleBot's own "Hey LittleBot" model (openWakeWord, trained on the robot) as a second check next to Vosk.

The model was trained with openWakeWord (docs/WAKE_TRAINING.md). It is run here with onnxruntime directly (not
the openwakeword package), with openWakeWord's own feature steps, matched to ``openwakeword.utils.AudioFeatures``
and ``openwakeword.Model.predict``:

1. 16 kHz 16-bit sound in chunks of 1280 samples (80 ms);
2. ``melspectrogram.onnx`` on the chunk and the 480 samples before it (8 frames of 32 mel bands; the samples are
   given as int16 values in float32, not scaled), then ``mel / 10 + 2``;
3. ``embedding_model.onnx`` on the last 76 mel frames: one 96-number embedding per chunk;
4. ``hey_little_bot.onnx`` on the last 16 embeddings (about 1.28 s): a score from 0 to 1. The first 5 scores after
   a reset are 0, as in openWakeWord (its buffers start with made-up values).

``WakeModelCheck`` does that on its own thread, on the same microphone sound Vosk gets (the wake word thread hands
it over without waiting). In ``wake.oww_mode: shadow`` it changes nothing: for each Vosk wake it logs the model's
best score around it, and it logs its own peaks over ``wake.oww_peak_threshold`` that Vosk didn't wake on; both
also go to ``data/wake/shadow.jsonl`` (numbers only: no sound, no words), with how the chat went after each wake.
``scripts/wake_shadow_report.py`` sums that file up. In ``confirm`` the wake word asks ``score_now`` before waking.

Anything wrong (a missing file, no onnxruntime, an error while scoring) switches it off with one warning; the wake
word then goes on exactly as without it.
"""

from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

log = logging.getLogger("littlebot.wake")

RATE = 16000
CHUNK = 1280  # 80 ms: openWakeWord's step
MEL_CONTEXT = 160 * 3  # samples before the chunk given to the mel model with it (openWakeWord: n_samples + 160*3)
MEL_BINS = 32
MEL_WINDOW = 76  # mel frames per embedding
EMBED_DIM = 96
WARMUP_SCORES = 5  # the first scores after a reset are 0 (openWakeWord: while its buffers fill)
NOISE_SECONDS = 4  # openWakeWord starts its embeddings from 4 s of quiet random sound

MODEL_FILE = "hey_little_bot.onnx"
MEL_FILE = "melspectrogram.onnx"
EMBED_FILE = "embedding_model.onnx"
SHADOW_FILE = "shadow.jsonl"
SHADOW_MAX_BYTES = 4_000_000  # then the file is moved to shadow.jsonl.1 and a new one started

FEED_SLACK = 1600  # a block handed over (0.1 s) may end just after the moment measured
QUEUE_BLOCKS = 60 # 0.1 s blocks waiting for the thread (6 s): more and the newest sound is skipped
AFTER_SECONDS = 0.6  # the model's score is also measured this long after Vosk's wake (an early wake is mid-phrase)
RESOLVE_TIMEOUT = 2.0  # ...or at most this long after it, if the sound stops (the call took the microphone)
CLAIM_SECONDS = 3.0  # a model peak with no Vosk wake this long after it is a model-only peak
UNSURE_SECONDS = 3.0  # Vosk heard a wake phrase, not sure enough, this close to a peak
CPU_NOTE_SECONDS = 60.0  # seconds of sound: then one line with the CPU it took
SUMMARY_SECONDS = 3600.0  # one summary line this often (while it hears sound)

_RESET = object()


def _first(out: Any) -> np.ndarray:
    """An onnxruntime ``run`` result (a list of outputs): the first one."""
    return np.asarray(out[0] if isinstance(out, (list, tuple)) else out)


class WakeScorer:
    """openWakeWord's streaming features and the classifier, one 80 ms chunk at a time (not thread-safe).

    ``melspec``, ``embed`` and ``classify`` take a float32 array and return the model's output (an onnxruntime
    session's ``run`` result, or the first output): ``melspec`` (1, N samples) -> (.., frames, 32), ``embed``
    (B, 76, 32, 1) -> (B, .., 96), ``classify`` (1, 16, 96) -> a score."""

    def __init__(self, melspec: Callable[[np.ndarray], Any], embed: Callable[[np.ndarray], Any],
                 classify: Callable[[np.ndarray], Any], *, frames: int = 16, seed: int = 0) -> None:
        self._melspec_run, self._embed_run, self._classify_run = melspec, embed, classify
        self.frames = int(frames)
        self._seed = seed
        self.reset()

    def reset(self) -> None:
        """Forget all sound (openWakeWord's ``AudioFeatures.reset`` and an empty prediction buffer)."""
        self._raw = np.zeros(0, dtype=np.int16)
        self._remainder = np.zeros(0, dtype=np.int16)
        self._mel = np.ones((MEL_WINDOW, MEL_BINS), dtype=np.float32)
        noise = np.random.default_rng(self._seed).integers(-1000, 1000, RATE * NOISE_SECONDS).astype(np.int16)
        self._feat = self._embed_clip(noise)
        self._scored = 0

    def _melspectrogram(self, x: np.ndarray) -> np.ndarray:
        spec = _first(self._melspec_run(x.astype(np.float32)[None, :]))
        return np.squeeze(spec).reshape(-1, MEL_BINS) / 10.0 + 2.0

    def _embed(self, windows: np.ndarray) -> np.ndarray:
        return _first(self._embed_run(windows.astype(np.float32))).reshape(-1, EMBED_DIM)

    def _embed_clip(self, x: np.ndarray) -> np.ndarray:
        """Embeddings of a whole clip (openWakeWord's ``_get_embeddings``: windows of 76 frames, 8 apart)."""
        spec = self._melspectrogram(x)
        windows = [spec[i:i + MEL_WINDOW] for i in range(0, spec.shape[0], 8) if i + MEL_WINDOW <= spec.shape[0]]
        return self._embed(np.expand_dims(np.array(windows), axis=-1))

    def process(self, pcm16: np.ndarray) -> list[float]:
        """Add sound (int16, 16 kHz); one score per whole 80 ms chunk completed (the rest waits for more)."""
        x = np.concatenate((self._remainder, np.asarray(pcm16, dtype=np.int16).reshape(-1)))
        whole = len(x) // CHUNK * CHUNK
        self._remainder = x[whole:]
        return [self._step(x[i:i + CHUNK]) for i in range(0, whole, CHUNK)]

    def _step(self, chunk: np.ndarray) -> float:
        self._raw = np.concatenate((self._raw, chunk))[-(CHUNK + MEL_CONTEXT):]
        # only the last 76 mel frames and 16 embeddings are ever used: openWakeWord keeps 10 s of each, the same
        # numbers with more copying
        self._mel = np.vstack((self._mel, self._melspectrogram(self._raw)))[-MEL_WINDOW:]
        window = self._mel.astype(np.float32)[None, :, :, None]
        self._feat = np.vstack((self._feat, self._embed(window)))[-self.frames:]
        score = float(_first(self._classify_run(self._feat[-self.frames:][None].astype(np.float32))).reshape(-1)[0])
        self._scored += 1
        return 0.0 if self._scored <= WARMUP_SCORES else score


def load_scorer(folder: Path) -> WakeScorer:
    """The three models in ``folder`` with onnxruntime (one thread each). Raises when something is missing."""
    missing = [n for n in (MODEL_FILE, MEL_FILE, EMBED_FILE) if not (folder / n).is_file()]
    if missing:
        raise FileNotFoundError(f"{', '.join(missing)} not in {folder}")
    import onnxruntime as ort  # noqa: PLC0415 - optional (pip install onnxruntime)

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1

    def session(name: str) -> Callable[[np.ndarray], Any]:
        sess = ort.InferenceSession(str(folder / name), sess_options=opts, providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0].name
        return lambda x: sess.run(None, {inp: x})

    cls = ort.InferenceSession(str(folder / MODEL_FILE), sess_options=opts, providers=["CPUExecutionProvider"])
    shape = cls.get_inputs()[0].shape
    frames = shape[1] if len(shape) > 1 and isinstance(shape[1], int) else 16
    cls_in = cls.get_inputs()[0].name
    return WakeScorer(session(MEL_FILE), session(EMBED_FILE), lambda x: cls.run(None, {cls_in: x}), frames=frames)


def _rss_mb() -> float | None:
    """This process's memory in MB (Linux), else None."""
    try:
        with open("/proc/self/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def call_outcome(last: dict[str, Any] | None) -> str:
    """How a chat after a wake went, from the voice's ``last_call`` (no words in it): request | dismissed |
    no_words | not_connected | unknown."""
    if not last:
        return "unknown"
    if last.get("connected") is False:
        return "not_connected"
    if last.get("heard_user") is False:
        return "no_words"
    if last.get("ended_reason") == "asked to stop" and int(last.get("turns") or 0) <= 1:
        return "dismissed"
    return "request"


OUTCOME_TEXT = {"request": "it became a real request", "dismissed": "it was dismissed at once",
                "no_words": "nobody said anything", "not_connected": "the chat did not connect",
                "not_started": "the chat could not start", "unknown": "how it went isn't known"}


class WakeModelCheck:
    """The trained model on its own thread: ``feed`` it the wake word's sound, ask it about Vosk's wakes."""

    def __init__(self, folder: Path, *, journal: Path | None = None, mode: str = "shadow",
                 peak_threshold: float = 0.5, window_seconds: float = 2.0,
                 load: Callable[[Path], WakeScorer] = load_scorer, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time) -> None:
        self.folder = Path(folder)
        self.journal = journal
        self.mode = mode
        self.peak_threshold = float(peak_threshold)
        self.window = int(float(window_seconds) * RATE)
        self._load = load
        self._clock, self._wall = clock, wall
        self._q: queue.Queue[Any] = queue.Queue(maxsize=QUEUE_BLOCKS)
        self._cv = threading.Condition()
        self._jlock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ready = False  # the models are loaded
        self.failed: str | None = None  # why it switched itself off
        self._fed = 0  # samples handed over (the wake word thread)
        self._done = 0  # samples scored (this thread)
        self._scores: collections.deque[tuple[int, float]] = collections.deque(maxlen=int(10 * RATE / CHUNK))
        self._wakes: list[dict[str, Any]] = []  # Vosk wakes waiting for the sound after them
        self._peak: dict[str, Any] | None = None  # a stretch over peak_threshold going on now
        self._peaks: list[dict[str, Any]] = []  # ...ended, not yet claimed by a Vosk wake
        self._unsure: collections.deque[int] = collections.deque(maxlen=20)
        self._dropped = 0
        self._cpu = 0.0
        self._sound = 0  # samples scored since the start (for the CPU line)
        self._cpu_noted = False
        self._load_ms = 0
        self._mem_mb: float | None = None
        self._hour = {"since": clock(), "sound": 0, "cpu": 0.0, "best": 0.0, "peaks": 0, "wakes": 0}
        self.counts = {"vosk_wakes": 0, "agreed": 0, "model_only": 0, "blocked": 0}

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is None and not self.failed:
            self._thread = threading.Thread(target=self._run, name="littlebot-wake-model", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(None)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)
        self._thread = None

    @property
    def working(self) -> bool:
        return self.failed is None and not self._stop.is_set()

    def switch_off(self, why: str) -> None:
        """One warning; from then on everything here does nothing."""
        if self.failed is None:
            self.failed = why
            log.warning("wake model: switched off (%s); the wake word goes on as before, with Vosk alone", why)
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    # ------------------------------------------------------------ from the wake word thread
    def feed(self, pcm16: np.ndarray) -> None:
        """The sound Vosk gets (int16, 16 kHz). Never waits; skipped while the thread is behind."""
        if not self.working:
            return
        n = len(pcm16)
        with self._cv:
            start = self._fed
            self._fed += n
        try:
            self._q.put_nowait((start, np.array(pcm16, dtype=np.int16, copy=True)))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                log.info("wake model: fell behind the microphone; some sound is not scored")

    def reset(self) -> None:
        """A new stretch of listening (Vosk was reset too): the sound before it doesn't count any more."""
        if not self.working:
            return
        with self._cv:
            at = self._fed
        with contextlib.suppress(queue.Full):
            self._q.put_nowait((_RESET, at))

    def vosk_unsure(self) -> None:
        """Vosk heard a wake phrase but wasn't sure enough (it didn't wake)."""
        if self.working:
            with self._cv:
                self._unsure.append(self._fed)

    def score_now(self, wait: float = 0.3) -> float | None:
        """The model's best score in the last window, once it has scored the sound handed over so far (at most
        ``wait`` seconds); None when it can't say (not loaded, switched off): then Vosk alone decides."""
        if not self.working or not self.ready:
            return None
        deadline = self._clock() + max(0.0, wait)
        with self._cv:
            target = self._fed
            while self._done < target and self.working:
                left = deadline - self._clock()
                if left <= 0:
                    break
                self._cv.wait(left)
            if not self.working:
                return None
            return self._best(target - self.window, target)

    def vosk_wake(self, vosk_conf: float, *, early: bool = False, video: bool = False,
                  blocked: bool = False, score: float | None = None) -> int | None:
        """Vosk woke (or, ``blocked``, would have but confirm stopped it): measured and logged once the model has
        scored a moment more of the sound (AFTER_SECONDS). The wake's id for ``outcome``, or None."""
        if not self.working:
            return None
        wid = int(self._wall() * 1000)
        with self._cv:
            at = self._fed
            self._wakes.append({"id": wid, "t": self._wall(), "from": at - self.window, "at": at,
                                "until": at + int(AFTER_SECONDS * RATE), "deadline": self._clock() + RESOLVE_TIMEOUT,
                                "conf": float(vosk_conf), "early": bool(early), "video": bool(video),
                                "blocked": bool(blocked), "score": score})
        return wid

    def outcome(self, wake_id: int | None, outcome: str, *, turns: int | None = None,
                seconds: float | None = None) -> None:
        """How the chat after a wake went (call_outcome): logged and kept in the journal."""
        if wake_id is None or self.failed:
            return
        log.info("wake model: after the wake at %s: %s%s", _clock_text(wake_id / 1000.0),
                 OUTCOME_TEXT.get(outcome, outcome),
                 f" ({turns} turn{'s' if turns != 1 else ''}, {seconds:.0f} s)" if turns is not None and seconds
                 is not None else "")
        rec: dict[str, Any] = {"t": _iso(self._wall()), "kind": "outcome", "id": wake_id, "outcome": outcome}
        if turns is not None:
            rec["turns"] = int(turns)
        if seconds is not None:
            rec["seconds"] = round(float(seconds), 1)
        self._write(rec)

    def status(self) -> dict[str, Any]:
        state = "off" if self.failed else ("ready" if self.ready else "loading")
        return {"mode": self.mode, "state": state, "detail": self.failed, **self.counts}

    # ------------------------------------------------------------ the thread
    def _run(self) -> None:
        try:
            before = _rss_mb()
            t0 = time.monotonic()
            scorer = self._load(self.folder)
            self._load_ms = int((time.monotonic() - t0) * 1000)
            after = _rss_mb()
            self._mem_mb = after - before if after is not None and before is not None else None
        except Exception as e:  # noqa: BLE001 - a missing file, no onnxruntime, a broken model ...
            self.switch_off(f"could not load it: {type(e).__name__}: {e}")
            return
        self.ready = True
        log.info("wake model: ready in %d ms (%s, memory %s); mode %s", self._load_ms, self.folder,
                 f"+{self._mem_mb:.0f} MB" if self._mem_mb is not None else "unknown", self.mode)
        try:
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.25)
                except queue.Empty:
                    item = None
                if item is None:
                    if self._stop.is_set():
                        break
                elif item[0] is _RESET:
                    self._settle(force=True)
                    scorer.reset()
                    with self._cv:
                        self._scores.clear()
                        self._done = max(self._done, int(item[1]))
                        self._cv.notify_all()
                else:
                    self._score(scorer, int(item[0]), item[1])
                self._settle()
                self._summary()
        except Exception as e:  # noqa: BLE001 - never takes the wake word with it
            self.switch_off(f"scoring failed: {type(e).__name__}: {e}")
        with contextlib.suppress(Exception):
            self._settle(force=True)
            self._summary(final=True)

    def _score(self, scorer: WakeScorer, start: int, pcm: np.ndarray) -> None:
        t0 = time.thread_time()
        scores = scorer.process(pcm)
        cpu = time.thread_time() - t0
        end = start + len(pcm)
        with self._cv:
            for s in scores:
                self._scores.append((end, s))
            self._done = max(self._done, end)
            self._cv.notify_all()
        for s in scores:
            self._track_peak(end, s)
        self._cpu += cpu
        self._sound += len(pcm)
        h = self._hour
        h["sound"] += len(pcm)
        h["cpu"] += cpu
        if scores:
            h["best"] = max(h["best"], max(scores))
        if not self._cpu_noted and self._sound >= CPU_NOTE_SECONDS * RATE:
            self._cpu_noted = True
            secs = self._sound / RATE
            log.info("wake model: over its first %.0f s of sound it took %.1f%% of one CPU core (%.1f ms per 80 ms); "
                     "memory %s", secs, 100.0 * self._cpu / secs, 1000.0 * self._cpu / (secs / 0.08),
                     f"+{self._mem_mb:.0f} MB" if self._mem_mb is not None else "unknown")

    def _best(self, lo: int, hi: int) -> float:
        """The best score of the sound between samples ``lo`` and ``hi`` (0 if none). Call with _cv held."""
        return max((s for end, s in self._scores if lo < end <= hi), default=0.0)

    def _track_peak(self, at: int, score: float) -> None:
        if score >= self.peak_threshold:
            if self._peak is None:
                self._peak = {"start": at, "end": at, "max": score}
            else:
                self._peak["end"], self._peak["max"] = at, max(self._peak["max"], score)
        elif self._peak is not None:
            self._close_peak()

    def _close_peak(self) -> None:
        if self._peak is not None:
            self._peak["closed"] = self._clock()
            self._peak["t"] = self._wall()
            self._peaks.append(self._peak)
            self._peak = None

    def _settle(self, *, force: bool = False) -> None:
        """Measure the Vosk wakes whose sound is in (or that waited long enough), and log the model's own peaks
        that no Vosk wake claimed."""
        now = self._clock()
        if force:
            self._close_peak()
        with self._cv:
            ready = [w for w in self._wakes if force or self._done >= w["until"] or now >= w["deadline"]]
            self._wakes = [w for w in self._wakes if w not in ready]
            measured = [(w, self._best(w["from"], w["at"]), self._best(w["from"], w["until"] + FEED_SLACK)) for w in ready]
            waiting = list(self._wakes)
        for w, at_wake, after in measured:
            self._log_wake(w, at_wake, after)
        keep = []
        for p in self._peaks:
            if any(p["end"] > w["from"] for w in waiting):
                keep.append(p)  # a Vosk wake measuring this sound will claim it
            elif now - p["closed"] >= CLAIM_SECONDS:
                self._log_peak(p)
            else:
                keep.append(p)
        self._peaks = keep

    def _log_wake(self, w: dict[str, Any], at_wake: float, after: float) -> None:
        # the model's peaks in this sound were this wake (not model-only)
        self._peaks = [p for p in self._peaks if not (p["end"] > w["from"] and p["start"] <= w["until"] + CHUNK)]
        if self._peak is not None and self._peak["end"] > w["from"]:
            self._peak["claimed"] = True
        self.counts["vosk_wakes"] += 1
        self._hour["wakes"] += 1
        if max(at_wake, after) >= self.peak_threshold:
            self.counts["agreed"] += 1
        if w["blocked"]:
            self.counts["blocked"] += 1
        what = ("would have woken it, but confirm stopped it" if w["blocked"] else "woke it")
        log.info("wake model: Vosk %s (word confidence %.2f%s); the model's score: %.2f at the wake, %.2f up to "
                 "%.1f s after%s", what, w["conf"], ", early" if w["early"] else "", at_wake, after, AFTER_SECONDS,
                 " (shadow: it changes nothing)" if self.mode == "shadow" else "")
        self._write({"t": _iso(w["t"]), "kind": "vosk_wake", "id": w["id"], "vosk_conf": round(w["conf"], 3),
                     "model_score": round(at_wake, 3), "model_score_after": round(after, 3), "early": w["early"],
                     "video": w["video"], "mode": self.mode, "blocked": w["blocked"]})

    def _log_peak(self, p: dict[str, Any]) -> None:
        if p.get("claimed"):
            return
        with self._cv:
            unsure = any(p["start"] - UNSURE_SECONDS * RATE <= u <= p["end"] + UNSURE_SECONDS * RATE
                         for u in self._unsure)
        self.counts["model_only"] += 1
        self._hour["peaks"] += 1
        log.info("wake model: score %.2f with no Vosk wake%s", p["max"],
                 " (Vosk heard a wake phrase but wasn't sure enough)" if unsure else "")
        self._write({"t": _iso(p["t"]), "kind": "model_only", "model_score": round(p["max"], 3),
                     "vosk_unsure": unsure})

    def _summary(self, *, final: bool = False) -> None:
        """Once an hour (and at the end): one line, and how much sound it heard in the journal (for the report's
        "per hour")."""
        h = self._hour
        now = self._clock()
        if now - h["since"] < SUMMARY_SECONDS and not final:
            return
        secs = h["sound"] / RATE
        if secs > 0:
            self._write({"t": _iso(self._wall()), "kind": "listened", "sound_s": round(secs, 1),
                         "vosk_wakes": h["wakes"], "model_only": h["peaks"]})
        if secs > 0 and not final:
            log.info("wake model: in the last hour: %.0f s of sound scored, highest score %.2f, %d Vosk wake%s, %d "
                     "model-only peak%s, %.1f%% of one CPU core", secs, h["best"], h["wakes"],
                     "" if h["wakes"] == 1 else "s", h["peaks"], "" if h["peaks"] == 1 else "s",
                     100.0 * h["cpu"] / secs)
        self._hour = {"since": now, "sound": 0, "cpu": 0.0, "best": 0.0, "peaks": 0, "wakes": 0}

    def _write(self, rec: dict[str, Any]) -> None:
        path = self.journal
        if path is None:
            return
        try:
            with self._jlock:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_file() and path.stat().st_size > SHADOW_MAX_BYTES:
                    path.replace(path.with_name(path.name + ".1"))
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError as e:
            log.debug("wake model: couldn't write %s: %s", path, e)


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t).isoformat(timespec="seconds")


def _clock_text(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S")


__all__ = ["WakeScorer", "WakeModelCheck", "load_scorer", "call_outcome"]
