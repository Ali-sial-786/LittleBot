"""``ctx.voices``: who is talking, by voice, among the family members a parent added (opt-in).

Nothing is recognised until the parent switches on "Recognise family voices"
(``privacy.voice_recognition_enabled``, off by default) on the Privacy page; the microphone switch must
be on too. Then:

* ``identify(samples, rate)``: whose voice this is (``VoiceMatch``), or None when LittleBot isn't sure,
  never a guess. A voice is someone's only when its voiceprint is close to the average of theirs (cosine
  similarity of at least ``voices.match_threshold``, 0.5) AND clearly closer to them than to anyone else
  (``MATCH_MARGIN``), from at least ``MIN_SPEECH_S`` of speech, pauses left out (shorter, down to
  ``SHORT_MIN_SPEECH_S``, only when the caller asks, with a stricter threshold);
* ``enrol(person_id, samples, rate)``: the dashboard's "Record voice" (three sentences, ~20 s);
* ``learn(person_id, samples, rate)``: in a chat, a grown-up said who they are ("I'm Ali");
  ``improve(heard)``: LittleBot was very sure it was them (``LEARN_SCORE``). Both are capped
  (``littlebot.voices.store``: at most 8 learned voiceprints a person, the oldest goes first);
* ``forget(person_id)``, ``status()``.

How: sherpa-onnx (``pip install -e '.[voices]'``) with 3D-Speaker's ERes2Net model
(``scripts/download_models.py voices``, 26 MB) turns up to ``MAX_EMBED_S`` of speech into 192 numbers.
The model is loaded on first use, in a worker thread, and dropped after ``UNLOAD_IDLE_S`` without use or
when switched off. "Hey LittleBot" loads it straight away (the wake word's ``audio.wake_word`` event), so
it is ready when the call wants to know who woke it.

Privacy: only voiceprints (numbers) are kept, in LittleBot's database (``voice_samples``), never a
recording: the sound is looked at in memory and dropped. The numbers never leave the robot and are never
logged, shown or sent; logs say counts and scores only. During a voice chat only the name and role of
who is talking go to the voice (OpenAI), as for faces. Switched off: nothing is processed at all.

Measured on an x86 dev machine (not the Pi): load ~0.3 s and ~75 MB, then ~50 ms for 1.5 s of speech and
~70 ms for 2 s with 2 threads (~+20 MB while computing 3 s). Expect roughly twice that on a Pi 5.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from littlebot.core.events import Event, Topics
from littlebot.core.service import Service, ServiceStatus
from littlebot.voices import store as vstore
from littlebot.voices.engine import (
    MAX_EMBED_S,
    RATE,
    EngineUnavailable,
    SpeakerEngine,
    sherpa_installed,
    speech_only,
    to_model_rate,
    unit,
    windows,
)

log = logging.getLogger("littlebot.voices")

MODEL = "models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
MATCH_THRESHOLD = 0.5  # default for voices.match_threshold (cosine similarity to a person's average)
MATCH_MARGIN = 0.10  # ...and at least this much closer to them than to the next person
MIN_SPEECH_S = 1.0  # identify() needs this much speech by default (pauses left out: about 1.5 s of talking)
SHORT_MIN_SPEECH_S = 0.6  # the least a caller may ask for (min_speech_s); below MIN_SPEECH_S the threshold is
SHORT_EXTRA = 0.05  # ...this much stricter
# Learning in chats: only from one clear voice (the two halves of what they said sound alike) ...
ONE_VOICE_MIN = 0.30
ONE_VOICE_MIN_SPEECH_S = 2.0  # ...which can only be told from this much speech (halves of 1 s)
CLAIM_MIN_SPEECH_S = 1.0  # "I'm Ali": at least this much of their speech in that turn (or the next one)
CLAIM_MIN_SCORE = 0.30  # ...and, if Ali has voiceprints already, not clearly a different voice
LEARN_SCORE = 0.62  # improve(): LittleBot is very sure (well above MATCH_THRESHOLD) ...
LEARN_MARGIN = 0.20  # ...and nobody else comes close
LEARN_MIN_SPEECH_S = 2.0
LEARN_GAP_S = 120.0  # at most one learned voiceprint per person this often
ENROL_MIN_SPEECH_S = 4.0  # the dashboard's recording: at least this much speech (reading all three: ~8-12 s)
ENROL_WINDOW_S = 3.0  # one voiceprint per this much of it ...
ENROL_MAX_SAMPLES = 4  # ...at most this many per recording
ENROL_CONSISTENT = 0.30  # every two pieces must sound alike (one voice; real speech: one person >= ~0.5, two <= ~0.2)
ENROL_MAX_ODD = 1 / 3  # up to this share of the pieces may be left out for sounding unlike the rest (a cough, the baby)
WAKE_TOPIC = "audio.wake_word"  # littlebot.audio.wakeword.WAKE_TOPIC: "Hey LittleBot" was heard
UNLOAD_IDLE_S = 600.0  # the model (~75 MB) is dropped after this long without use
RECORD_SECONDS = 20.0  # the dashboard's recording
SENTENCES = (
    "Hello LittleBot, it's me. I'm reading this so that you can learn my voice.",
    "The little duck swam across the pond to find her friends, splashing all the way.",
    "What's the weather like today? Shall we play a game, read a book or sing a song?",
)

ERRORS = {
    "off": "Recognising family voices is switched off. Turn it on in Privacy.",
    "microphone_off": "The microphone is turned off in Privacy.",
    "no_package": "The voice recognition package isn't installed yet. On the robot, run: "
                  ".venv/bin/python -m pip install -e '.[voices]'",
    "no_model": "The voice model isn't downloaded yet. On the robot, run: "
                ".venv/bin/python scripts/download_models.py voices",
    "load_failed": "The voice model couldn't be loaded. Download it again: "
                   ".venv/bin/python scripts/download_models.py voices --force, then switch it off and on.",
}
# Why a recording wasn't kept, for the parent ({name}: the person; {other}: who it sounds like instead).
ENROL_ERRORS = {
    "too_little": "LittleBot heard only {secs:.0f} seconds of talking. Read all three sentences out loud, about an "
                  "arm's length from the robot, then try again.",
    "many_voices": "That didn't sound like one voice (more than one person, or a lot of noise). Make sure only "
                   "{name} talks, in a quiet room, then try again.",
    "looks_like_other": "That sounds more like {other} than {name}, so it wasn't kept. Make sure only {name} talks, "
                        "then try again.",
}


@dataclasses.dataclass(frozen=True)
class VoiceMatch:
    """Whose voice it is (``is_child``: the little one, by the same rule as faces), and how close."""

    person_id: int
    name: str
    role: str
    score: float
    is_child: bool
    margin: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.name} ({self.role})" if self.role else self.name


@dataclasses.dataclass(frozen=True)
class Heard:
    """What one stretch of sound told: how much speech, its voiceprint (never shown or sent), who it is.

    ``reason``: ok (``match`` is set) | unsure | too_short | nobody (nobody has a voiceprint) | a blocked
    reason (``ERRORS``). ``best_id`` / ``best``: the closest person and score even when unsure.
    ``one_voice``: the two halves sound alike (None: too short to tell)."""

    reason: str
    speech_s: float = 0.0
    match: VoiceMatch | None = None
    best_id: int | None = None
    best: float = 0.0
    second: float = -1.0
    one_voice: bool | None = None
    voiceprint: np.ndarray | None = dataclasses.field(default=None, repr=False, compare=False)

    def score_for(self, voice: vstore.Voice | None) -> float | None:
        if voice is None or self.voiceprint is None or voice.centre.size != self.voiceprint.size:
            return None
        return float(np.dot(self.voiceprint, voice.centre))


def best_match(voiceprint: np.ndarray, known: list[vstore.Voice], *, threshold: float = MATCH_THRESHOLD,
               margin: float = MATCH_MARGIN) -> tuple[vstore.Voice | None, float, float, vstore.Voice | None]:
    """(who, best score, second-best score, closest) - who is None unless close AND clearly closer than anyone else."""
    scores = sorted(((float(np.dot(voiceprint, k.centre)), n) for n, k in enumerate(known)
                     if k.centre.size == voiceprint.size), reverse=True)
    if not scores:
        return None, 0.0, -1.0, None
    best, idx = scores[0]
    second = scores[1][0] if len(scores) > 1 else -1.0
    closest = known[idx]
    return (closest if best >= threshold and best - second >= margin else None), best, second, closest


class VoiceRecognition(Service):
    """The family's voices (``ctx.voices``, ``get_voices(ctx)``), shared by live voice and the dashboard.

    Thread-safe: ``analyse_now`` / ``identify_now`` may be called from any thread (one voiceprint is
    computed at a time); the async methods run them in a worker thread."""

    name = "voices"

    def __init__(self, ctx: Any, *, engine: Any = None, root: Path | None = None) -> None:
        super().__init__()
        self.ctx = ctx
        self._engine = engine  # injected (tests) or loaded on first use
        self._injected = engine is not None
        self._root = root
        model = Path(self._model_setting()).name if engine is None else str(getattr(engine, "name", "test"))
        self.store = vstore.VoiceStore(ctx.db, model)
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._package: tuple[bool, float] | None = None  # (installed, when checked)
        self._last_used = 0.0
        self._unload_at: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._learned_at: dict[int, float] = {}
        self._subs = [ctx.bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
                      ctx.bus.subscribe(WAKE_TOPIC, self._on_wake)]

    # -- lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.set_status(ServiceStatus.RUNNING)

    async def stop(self) -> None:
        for sub in self._subs:
            try:
                sub.cancel()
            except Exception:  # noqa: BLE001
                pass
        self._subs = []
        self.unload("stopping")
        self.set_status(ServiceStatus.STOPPED)

    # -- gates
    def enabled(self) -> bool:
        return bool(getattr(self.ctx.privacy, "voice_recognition_enabled", False))

    @property
    def threshold(self) -> float:
        value = getattr(getattr(self.ctx.config, "voices", None), "match_threshold", MATCH_THRESHOLD)
        try:
            return min(0.9, max(0.3, float(value)))
        except (TypeError, ValueError):
            return MATCH_THRESHOLD

    def blocked_reason(self) -> str | None:
        """Why recognising isn't possible now (a key of ``ERRORS``), or None. Checked before any sound is looked at."""
        if not self.enabled():
            return "off"
        if not self.ctx.privacy.microphone_enabled:
            return "microphone_off"
        return self.models_problem()

    def _model_setting(self) -> str:
        return str(getattr(getattr(self.ctx.config, "voices", None), "model", "") or MODEL)

    def model_path(self) -> Path:
        from littlebot.core.paths import PROJECT_ROOT  # noqa: PLC0415

        p = Path(self._model_setting()).expanduser()
        if self._root is not None:
            return self._root / p.name
        return p if p.is_absolute() else PROJECT_ROOT / p

    def models_problem(self) -> str | None:
        """no_package | no_model | load_failed, or None (cheap: no loading)."""
        if self._injected:
            return None
        if self._load_error:
            return self._load_error
        now = time.monotonic()
        if self._package is None or (not self._package[0] and now - self._package[1] > 10.0):
            self._package = (sherpa_installed(), now)
        if not self._package[0]:
            return "no_package"
        if not self.model_path().is_file():
            return "no_model"
        return None

    def status(self) -> dict[str, Any]:
        reason = self.blocked_reason()
        problem = self.models_problem()
        return {"enabled": self.enabled(), "blocked": reason, "blocked_detail": ERRORS.get(reason) if reason else None,
                "models_ready": problem is None, "models_detail": ERRORS.get(problem) if problem else None,
                "loaded": self._engine is not None, "threshold": round(self.threshold, 3),
                "people_with_voices": len(self.store.known())}

    def view(self) -> dict[str, Any]:
        """For the dashboard's Who's who: the status, counts per person and the sentences to read (never numbers)."""
        return {**self.status(), "counts": {str(k): v for k, v in self.store.counts().items()},
                "sentences": list(SENTENCES), "record_seconds": RECORD_SECONDS,
                "max_recorded": vstore.MAX_RECORDED, "max_learned": vstore.MAX_LEARNED}

    # -- the model
    def engine(self) -> Any:
        """The model, loaded on first use (call from a worker thread: ~0.3-1 s and ~75 MB of RAM)."""
        with self._lock:  # one load at a time
            engine = self._engine
            if engine is not None:
                return engine
            problem = self.models_problem()
            if problem:
                raise EngineUnavailable(problem)
            t0 = time.perf_counter()
            threads = int(getattr(getattr(self.ctx.config, "voices", None), "threads", 2) or 2)
            try:
                engine = SpeakerEngine(self.model_path(), threads=threads)
            except EngineUnavailable as e:
                if e.reason == "load_failed":
                    self._load_error = "load_failed"
                    log.warning("voice recognition: the voice model couldn't be loaded")
                raise
            log.info("voice recognition: model loaded in %d ms", int((time.perf_counter() - t0) * 1000))
            if self.enabled():  # not when it was switched off meanwhile: this one use only
                self._engine = engine
            return engine

    def unload(self, why: str) -> None:
        self._load_error = None
        if self._injected or self._engine is None:
            return
        self._engine = None
        log.info("voice recognition: model unloaded (%s)", why)

    def _remember_loop(self) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

    def _used(self) -> None:
        """Keep the model a while after each use; drop it after UNLOAD_IDLE_S without one."""
        self._last_used = time.monotonic()
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._arm_unload()
        else:
            loop.call_soon_threadsafe(self._arm_unload)

    def _arm_unload(self) -> None:
        if self._unload_at is not None:
            self._unload_at.cancel()
        self._unload_at = asyncio.get_running_loop().call_later(UNLOAD_IDLE_S, self._unload_if_idle)

    def _unload_if_idle(self) -> None:
        self._unload_at = None
        if time.monotonic() - self._last_used >= UNLOAD_IDLE_S - 1:
            self.unload(f"not used for {UNLOAD_IDLE_S / 60:g} minutes")

    def _on_privacy(self, event: Event) -> None:
        if not self.enabled() or not self.ctx.privacy.microphone_enabled:
            self.unload("switched off")

    def _on_wake(self, event: Event) -> None:
        """"Hey LittleBot": load the model now (in a worker thread), so it is ready to tell who woke it."""
        loop = self._loop
        if self.blocked_reason() is not None or self._engine is not None or loop is None or loop.is_closed() \
                or not self.store.has_samples():
            return
        loop.call_soon_threadsafe(lambda: loop.run_in_executor(None, self._warm))

    def _warm(self) -> None:
        try:
            self.engine()
            self._used()
        except EngineUnavailable:
            pass
        except Exception:  # noqa: BLE001 - only getting ready
            log.debug("voice recognition: warming up failed", exc_info=True)

    # -- people
    def _child(self) -> str | None:
        from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

        try:
            profile = self.ctx.settings.get("profile") or {}
            return clean_child_name(profile.get("nickname")) if isinstance(profile, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _match(self, voice: vstore.Voice, score: float, margin: float) -> VoiceMatch:
        from littlebot.vision.faces import is_child  # noqa: PLC0415

        return VoiceMatch(voice.id, voice.name, voice.role, round(score, 3), is_child(voice.name, voice.role,
                                                                                       self._child()), round(margin, 3))

    # -- listening (blocking: any thread)
    def analyse_now(self, samples: np.ndarray, rate: int, *, min_speech_s: float = MIN_SPEECH_S, take: str = "last",
                    one_voice: bool = False) -> Heard:
        """Whose voice ``samples`` is (see ``Heard``). ``take``: which MAX_EMBED_S of the speech ("first" / "last")
        makes the voiceprint; ``one_voice``: also check that it is one voice (two more voiceprints)."""
        reason = self.blocked_reason()
        if reason is not None:
            return Heard(reason)  # switched off: the sound isn't even looked at
        known = self.store.known()
        x = speech_only(to_model_rate(samples, rate))
        speech_s = len(x) / RATE
        min_speech_s = max(SHORT_MIN_SPEECH_S, float(min_speech_s))
        if speech_s < min_speech_s:
            return Heard("too_short", speech_s)
        n = int(MAX_EMBED_S * RATE)
        clip = x[:n] if take == "first" else x[-n:]
        engine = self.engine()
        voiceprint = engine.embed(clip)
        same = None
        if one_voice and speech_s >= ONE_VOICE_MIN_SPEECH_S:
            half = len(clip) // 2
            same = float(np.dot(engine.embed(clip[:half]), engine.embed(clip[half:]))) >= ONE_VOICE_MIN
        del x, clip
        if not known:
            return Heard("nobody", speech_s, one_voice=same, voiceprint=voiceprint)
        threshold = self.threshold + (SHORT_EXTRA if speech_s < MIN_SPEECH_S else 0.0)
        who, best, second, closest = best_match(voiceprint, known, threshold=threshold, margin=MATCH_MARGIN)
        match = self._match(who, best, best - second) if who is not None else None
        return Heard("ok" if match else "unsure", speech_s, match, closest.id if closest else None, best, second,
                     same, voiceprint)

    def identify_now(self, samples: np.ndarray, rate: int, *, min_speech_s: float = MIN_SPEECH_S) -> VoiceMatch | None:
        try:
            return self.analyse_now(samples, rate, min_speech_s=min_speech_s).match
        except EngineUnavailable:
            return None

    # -- listening (async)
    async def analyse(self, samples: np.ndarray, rate: int, **kw: Any) -> Heard:
        """``analyse_now`` in a worker thread (never raises for a missing or broken model: ``Heard(reason)``)."""
        reason = self.blocked_reason()
        if reason is not None:
            return Heard(reason)
        self._remember_loop()
        try:
            heard = await asyncio.to_thread(self.analyse_now, samples, rate, **kw)
        except EngineUnavailable as e:
            return Heard(e.reason)
        if heard.reason in ("ok", "unsure", "nobody"):
            self._used()
        return heard

    async def identify(self, samples: np.ndarray, rate: int, *, min_speech_s: float = MIN_SPEECH_S) -> VoiceMatch | None:
        """Whose voice this is, or None (switched off, nobody has a voiceprint, too little speech, or unsure).

        ``samples``: mono float (-1..1) or int16 at ``rate``; resampled to 16 kHz here. Only the last
        MAX_EMBED_S of speech is used. ~1-2 s of speech is enough; ``min_speech_s`` may be lowered to
        SHORT_MIN_SPEECH_S (then stricter). Nothing is kept."""
        return (await self.analyse(samples, rate, min_speech_s=min_speech_s)).match

    # -- learning
    async def enrol(self, person_id: int, samples: np.ndarray, rate: int) -> dict[str, Any]:
        """The dashboard's recording of ``person_id`` reading the sentences: keep up to ENROL_MAX_SAMPLES
        voiceprints (never the sound) if it is clearly one voice, theirs."""
        person = self.store.people.person(person_id)
        if person is None:
            raise KeyError("That person isn't on the list.")
        reason = self.blocked_reason()
        if reason is not None:
            raise EngineUnavailable(reason)
        self._remember_loop()
        try:
            prints = await asyncio.to_thread(self._enrol_prints, samples, rate)
        except EngineUnavailable as e:
            raise EngineUnavailable(e.reason) from None
        self._used()
        speech_s, vs = prints
        error, other = None, None
        if speech_s < ENROL_MIN_SPEECH_S or not vs:
            error = "too_little"
        else:
            vs, scores, worst = one_voice_pieces(vs)
            log.info("voice recognition: recording of %.1f s of speech: %d piece(s), each like the rest %s, %d left "
                     "out, closest two apart %.2f (one voice: at least %.2f)", speech_s, len(scores),
                     [round(x, 2) for x in scores], len(scores) - len(vs), worst, ENROL_CONSISTENT)
            centre = unit(np.mean(vs, axis=0))
            if worst < ENROL_CONSISTENT:
                error = "many_voices"
            else:
                known = self.store.known()
                own = next((k for k in known if k.id == person["id"]), None)
                mine = float(np.dot(centre, own.centre)) if own is not None and own.centre.size == centre.size else -1.0
                rivals = [(float(np.dot(centre, k.centre)), k) for k in known
                          if k.id != person["id"] and k.centre.size == centre.size]
                close = max(rivals, key=lambda r: r[0], default=None)
                if close is not None and close[0] >= self.threshold and close[0] > mine:
                    error, other = "looks_like_other", close[1].name
        if error is not None:
            log.info("voice recognition: recording not kept (%s; %.1f s of speech)", error, speech_s)
            return {"ok": False, "error": error, "person": person, "speech_s": round(speech_s, 1),
                    "detail": ENROL_ERRORS[error].format(name=person["name"], other=other, secs=speech_s)}
        for v in vs[:ENROL_MAX_SAMPLES]:
            count = self.store.add(person["id"], v, vstore.RECORDED)
        log.info("voice recognition: recording kept (%d voiceprint(s) from %.1f s of speech; %d for that person)",
                 min(len(vs), ENROL_MAX_SAMPLES), speech_s, count)
        return {"ok": True, "samples": count, "speech_s": round(speech_s, 1), "person": person}

    def _enrol_prints(self, samples: np.ndarray, rate: int) -> tuple[float, list[np.ndarray]]:
        x = speech_only(to_model_rate(samples, rate))
        speech_s = len(x) / RATE
        if speech_s < ENROL_MIN_SPEECH_S:
            return speech_s, []
        engine = self.engine()
        pieces = windows(x, ENROL_WINDOW_S, min_seconds=MIN_SPEECH_S)
        return speech_s, [engine.embed(w) for w in pieces]

    async def learn(self, person_id: int, samples: np.ndarray, rate: int) -> dict[str, Any]:
        """In a chat, a grown-up said they are ``person_id`` ("I'm Ali"): keep a voiceprint of what they just
        said, when it is one clear voice and enough speech (and, if they have voiceprints already, not clearly
        someone else's voice). ``{ok, reason, first, samples}``; ``first``: their first voiceprint."""
        heard = await self.analyse(samples, rate, min_speech_s=CLAIM_MIN_SPEECH_S, one_voice=True)
        return self.keep_claimed(person_id, heard)

    def keep_claimed(self, person_id: int, heard: Heard) -> dict[str, Any]:
        """``learn`` for sound already analysed (``analyse(..., one_voice=True)``)."""
        person = self.store.people.person(person_id)
        if person is None:
            return {"ok": False, "reason": "unknown_person"}
        from littlebot.vision.faces import is_child  # noqa: PLC0415

        if is_child(person["name"], person["role"], self._child()):
            return {"ok": False, "reason": "child"}  # the little one doesn't say "I'm ..."; a grown-up joking
        if heard.voiceprint is None:
            return {"ok": False, "reason": heard.reason}
        if heard.speech_s < CLAIM_MIN_SPEECH_S:
            return {"ok": False, "reason": "too_short"}
        if heard.one_voice is False:
            return {"ok": False, "reason": "many_voices"}
        if heard.match is not None and heard.match.person_id != person["id"]:
            return {"ok": False, "reason": "someone_else"}
        own = next((k for k in self.store.known() if k.id == person["id"]), None)
        score = heard.score_for(own)
        if score is not None and score < CLAIM_MIN_SCORE:
            return {"ok": False, "reason": "different_voice"}
        count = self.store.add(person["id"], heard.voiceprint, vstore.LEARNED)
        self._learned_at[person["id"]] = time.monotonic()
        log.info("voice recognition: learned a voiceprint in a chat (they said who they are; %d for that person)",
                 count)
        return {"ok": True, "reason": "claimed", "first": own is None, "samples": count}

    def improve(self, heard: Heard) -> bool:
        """LittleBot was very sure who was talking (LEARN_SCORE, LEARN_MARGIN, one voice, enough speech):
        keep that voiceprint too (at most one per person every LEARN_GAP_S; the oldest learned one goes)."""
        m = heard.match
        if m is None or heard.voiceprint is None or m.is_child or heard.one_voice is not True \
                or heard.speech_s < LEARN_MIN_SPEECH_S or m.score < LEARN_SCORE or m.margin < LEARN_MARGIN \
                or self.blocked_reason() is not None:
            return False
        now = time.monotonic()
        last = self._learned_at.get(m.person_id)
        if last is not None and now - last < LEARN_GAP_S:
            return False
        try:
            count = self.store.add(m.person_id, heard.voiceprint, vstore.LEARNED)
        except KeyError:  # deleted meanwhile
            return False
        self._learned_at[m.person_id] = now
        log.info("voice recognition: voiceprint improved in a chat (score %.2f; %d for that person)", m.score, count)
        return True

    def forget(self, person_id: int) -> int:
        """Delete every voiceprint of ``person_id``; how many."""
        gone = self.store.forget(person_id)
        self._learned_at.pop(int(person_id), None)
        log.info("voice recognition: a person's voiceprints were deleted (%d)", gone)
        return gone


def get_voices(ctx: Any) -> VoiceRecognition:
    """The one VoiceRecognition (``ctx.voices``), made on first use if the robot didn't build it."""
    voices = getattr(ctx, "voices", None)
    if voices is None:
        voices = ctx.voices = VoiceRecognition(ctx)
    return voices


def one_voice_pieces(vs: list[np.ndarray]) -> tuple[list[np.ndarray], list[float], float]:
    """The recording's voiceprints that sound like one voice: ``(kept, scores, worst)``. ``scores``: each piece
    against the average of the others. Pieces unlike the rest (below ENROL_CONSISTENT: a cough, the baby, a door)
    are left out, the least alike first, up to ENROL_MAX_ODD of them; ``worst`` is then the least alike two kept
    (1.0 for one piece). Two people talking in turns leave more than that unlike each other: still below."""
    if len(vs) < 2:
        return list(vs), [1.0] * len(vs), 1.0
    total = np.sum(vs, axis=0)
    scores = [float(np.dot(v, unit(total - v))) for v in vs]
    odd = sorted((i for i, x in enumerate(scores) if x < ENROL_CONSISTENT), key=lambda i: scores[i])
    odd = odd[:int(len(vs) * ENROL_MAX_ODD)]
    kept = [v for i, v in enumerate(vs) if i not in odd]
    worst = min((float(np.dot(a, b)) for i, a in enumerate(kept) for b in kept[i + 1:]), default=1.0)
    return kept, scores, worst


__all__ = [
    "ENROL_ERRORS",
    "ERRORS",
    "MATCH_MARGIN",
    "MATCH_THRESHOLD",
    "MIN_SPEECH_S",
    "SENTENCES",
    "Heard",
    "VoiceMatch",
    "VoiceRecognition",
    "best_match",
    "get_voices",
    "one_voice_pieces",
]
