"""Wake word: say "Hey LittleBot" (or "Hey Bot") and LittleBot starts a live voice call, hands-free.

Keyword spotting runs on the robot with Vosk (offline Kaldi speech recognition) and its small
English model (``vosk-model-small-en-us-0.15``: 40 MB download, ~130 MB RAM, shared with the
local STT when both use it). The recogniser runs in *grammar mode*: it may only choose between
the wake phrases, a list of common everyday words (``DISTRACTOR_WORDS``, so that "hey bob" or
"hey look" have somewhere better to go than "hey bot") and ``[unk]`` (anything else). That is
cheap - a few percent of one CPU core - and far less prone to false triggers than dictation.
Audio is processed in memory, 0.1 s at a time; only the sentence the recogniser is still deciding
on is kept (at most ``PREROLL_MAX_SECONDS``, in memory), and forgotten as soon as it decides that
sentence held no wake phrase. Nothing leaves the robot until a wake phrase was heard and live
voice (cloud) starts.

Why Vosk and not openWakeWord: openWakeWord's pretrained models cover other phrases ("alexa",
"hey jarvis", "hey mycroft" ...); "hey little bot" would need a custom model trained on
thousands of synthetic clips first. Vosk recognises any phrase made of words its model knows,
without training, and the phrases can be changed in ``wake.phrases``.

Sharing the microphone
    The USB microphone is an exclusive ALSA device, so the listener never opens a stream of its
    own: it reads the AudioService's input backend (``audio.background_input()``) while nothing
    else needs it. Before ``listen()``, the microphone test, ``suspend()`` (live voice) and its
    own ``stop()`` the AudioService calls ``release_mic()``, which returns once this listener
    has closed the stream; ``mic_free()`` hands it back. The listener also pauses while live
    voice is on, while the privacy microphone switch or the ``wake.enabled`` setting is off; while
    LittleBot itself makes sound (half-duplex) the audio is discarded.

While a video plays
    It keeps listening (``wake.during_videos``), but the video's own sound reaches the microphone
    too (there is no echo cancellation), so stricter rules apply: only ``wake.video_phrases``
    count ("hey little bot"), each word needs ``wake.video_min_confidence``, and with
    ``wake.video_duck`` the video is turned down for a moment after a "hey ...". What was heard
    but didn't count is logged ("ignored while a video plays"), to check on the robot.

When a wake phrase is heard
    During a video, the video is paused first (``ctx.media.hold``: its position is kept, the player
    closes and the face comes back), so the earcon is heard over a quiet speaker. Then face listening
    (RobotState.LISTENING) -> "listen" earcon (two soft rising tones; none when they kept talking,
    below) -> ``ctx.live.start_session(parent_present=True, carry=...)``; live voice then takes the
    microphone. If live voice can't start (not signed in, cloud off ...) or doesn't connect,
    LittleBot plays "uhoh" and logs why. No new trigger for ``wake.cooldown_seconds``. A paused video
    carries on by itself when the chat couldn't start or nobody said anything in it (most likely the
    wake word heard the video); otherwise it waits for "keep watching" / "stop the video" (the live
    voice handles those) and stops ``media.chat_pause_minutes`` after the chat.

Carrying what was said into the call ("Hey LittleBot, what's the weather today?" in one breath)
    The recogniser reports a wake phrase only once the sentence has ended, so the rest of that
    sentence comes from the kept audio: from where the wake phrase ends (the recogniser's word
    times), the wake phrase itself left out. The listener then keeps the stream and goes on
    collecting what they say, without what it hears while LittleBot makes a sound, until live
    voice takes the microphone (``release_mic``). The call sends it first, at real-time pace,
    and catches up with real time by shortening the pauses (littlebot/chatgpt/live_audio.py,
    ``CarryFeed``). If the recogniser heard more words after the wake phrase, they are talking:
    no earcon, live voice starts at once. In memory only (littlebot/audio/carry.py); dropped if
    live voice can't start, doesn't connect, or when the call ends. Not during a video: the
    video's own sound reaches the microphone too, so nothing is carried then.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import numpy as np

from littlebot.audio import devices as dev
from littlebot.audio import dsp
from littlebot.audio.carry import QUIET_RATIO, PreRoll, SpeechCarry
from littlebot.audio.devices import SoundDeviceInput
from littlebot.audio.errors import AudioDeviceError
from littlebot.audio.stt.vosk import load_model, model_present, resolve_model_path, vosk_installed
from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.paths import PROJECT_ROOT
from littlebot.core.service import Service, ServiceStatus
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.wake")

WAKE_TOPIC = "audio.wake_word"  # {phrase} published when a wake phrase was heard

READ_SECONDS = 0.1  # microphone read timeout
FEED_SAMPLES = 1600  # hand the recogniser 0.1 s at a time (16 kHz)
GATE_POLL_SECONDS = 0.5  # re-check "should I listen?" this often
RELEASE_TIMEOUT = 2.0  # release_mic() waits at most this long for the stream to close
RETRY_SECONDS = 5.0  # after a microphone error
AVAILABILITY_RECHECK = 10.0  # look for a newly downloaded model this often
MIN_LISTEN_SECONDS = 0.3  # a listen that ends sooner is followed by a short pause (no busy loop)
CONNECT_GRACE_SECONDS = 10.0  # beyond chatgpt.live_connect_timeout_seconds
# The earcon is 0.27 s: a speaker that hangs never keeps live voice (and so the wake word) waiting longer.
EARCON_TIMEOUT = 8.0
# "Hey LittleBot, what's the weather?" in one breath: the recogniser only reports the wake phrase once the
# sentence has ended, so the sentence being recognised is kept (in memory, at most this long) and what was
# said after the wake phrase goes into the call (littlebot/audio/carry.py).
PREROLL_MAX_SECONDS = 8.0
WAKE_CUT_MARGIN = 0.03  # start the carried words this much before the wake phrase's end (the recogniser's step)
CARRY_WAIT_SECONDS = 5.0  # after the wake phrase: keep what they say this long at most until live voice takes over
# Live voice opens its own stream once this one is closed; what is said in between (~0.1-0.3 s) is lost. So
# while they are talking it gets the microphone at their next short pause, after at most HANDOVER_WAIT_SECONDS
# (in a noisy room there may be none: that is what the wait can cost).
HANDOVER_QUIET_SECONDS = 0.1
HANDOVER_WAIT_SECONDS = 0.4  # well within RELEASE_TIMEOUT

# Pauses after which the model is unloaded (they usually last long; ~130 MB back for a video).
_LONG_PAUSES = {"disabled", "microphone_off", "video", "stopped"}
# Why a call that paused a video ended without anyone talking (the video then carries on).
NO_WORDS_REASON = "nobody spoke (the wake word may have heard the video)"

# Common words offered to the recogniser next to the wake phrases. Measured with espeak-ng
# voices: 48 of 184 "near miss" sentences ("hey bob", "hey look at that", "hey baby" ...)
# triggered with only the phrases + [unk]; 9 with this list, and none for "hey little bot".
DISTRACTOR_WORDS: tuple[str, ...] = tuple(dict.fromkeys("""
a about all and are as at back be because big bit boat body book boss box boy bus but by call can car cat
come could day did do dog done down eat fun get girl give go good got had has have he her here him his
home how i if in is it just know like lot made make man many me more my new no not now of off oh okay on
one only or our out over people play put ready right said see she so some stop take tell than that the
them then there they thing think this time to too two up us very want was water way we well went were what
when where which who why will with would yes you your bath bed ball bear bird blue bubbles bump cup daddy
mommy mama papa nana food hug kiss milk nap night please sorry thank toy uh um wow yeah bad bag bat bet
bought boot bud pot hot top tot pop dot bobby bob baby buddy mom dad look robot bottle hi hello hey little
""".split()))

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_NON_WORD = re.compile(r"[^a-z' ]+")
_COMPOUNDS = {"littlebot": "little bot"}  # words the model doesn't know, as it hears them

PAUSE_TEXT: dict[str, str] = {
    "starting": "Starting…",
    "stopped": "Stopped.",
    "disabled": "Off.",
    "microphone_off": "Paused: the microphone is turned off in Privacy.",
    "mic_in_use": "Paused while LittleBot uses the microphone.",
    "mic_error": "Paused: the microphone isn't working right now; trying again in a few seconds.",
    "live_voice": "Paused while live voice is on.",
    "video": "Paused while a video plays (wake.during_videos is off).",
    "starting_live": "Heard you! Starting live voice…",
    "cooldown": "Back in a moment…",
    "no_audio": "Not available: the audio system isn't running.",
    "no_microphone": "Paused: no microphone found (LittleBot is using a simulated microphone).",
}


class KeywordRecognizer(Protocol):
    """Streaming recogniser: 16-bit mono PCM in, a final result dict when an utterance ended."""

    sample_rate: int

    def accept(self, pcm16: bytes) -> dict[str, Any] | None:
        """``{"text": "...", "result": [{"word", "conf"}, ...]}`` when an utterance just ended, else None."""

    def reset(self) -> None: ...

    # Optional: ``partial() -> str``, the words of the utterance so far (for ``wake.video_duck``).


def normalise_phrase(text: str) -> str:
    """'Hey, LittleBot!' -> 'hey little bot' (lower case, words only, compounds split)."""
    t = _NON_WORD.sub(" ", _CAMEL.sub(" ", str(text or "")).lower())
    words: list[str] = []
    for w in t.split():
        words.extend(_COMPOUNDS.get(w, w).split())
    return " ".join(words)


def build_grammar(phrases: Sequence[str], *, known: Callable[[str], bool] | None = None,
                  distractors: bool = True) -> tuple[list[str], list[tuple[str, tuple[str, ...]]], list[str]]:
    """(Vosk grammar, usable phrases as (phrase, words), dropped phrases with unknown words)."""
    usable: list[tuple[str, tuple[str, ...]]] = []
    dropped: list[str] = []
    for phrase in dict.fromkeys(normalise_phrase(p) for p in phrases):
        words = tuple(phrase.split())
        if not words:
            continue
        if known is not None and not all(known(w) for w in words):
            dropped.append(phrase)
            continue
        usable.append((phrase, words))
    extra = [w for w in DISTRACTOR_WORDS if known is None or known(w)] if distractors else []
    grammar = list(dict.fromkeys([p for p, _ in usable] + extra)) + ["[unk]"]
    return grammar, usable, dropped


def match_wake_phrase(result: dict[str, Any], phrases: Sequence[tuple[str, tuple[str, ...]]],
                      min_confidence: float = 0.0) -> str | None:
    """The wake phrase heard in a recogniser result (consecutive words, each confident enough)."""
    words = result.get("result")
    if isinstance(words, list) and words:
        items = [w for w in words if isinstance(w, dict)]
        toks = [str(w.get("word", "")).lower() for w in items]
        confs = [float(w.get("conf", 1.0)) for w in items]
    else:
        toks = str(result.get("text", "")).lower().split()
        confs = [1.0] * len(toks)
    for phrase, ph in sorted(phrases, key=lambda p: -len(p[1])):  # "hey little bot" before "hey bot"
        n = len(ph)
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) == ph and min(confs[i:i + n]) >= min_confidence:
                return phrase
    return None


def wake_phrase_end(result: dict[str, Any], words: Sequence[str],
                    min_confidence: float = 0.0) -> tuple[float | None, int]:
    """(Where the wake phrase ``words`` ends in a recogniser result, in the recogniser's seconds; how many
    words were heard after it). The time is None without word timings. Vosk counts its seconds from the
    recogniser's first sample, also across ``reset()``; a first word's start includes the silence before
    it, its end is accurate."""
    items = [w for w in result.get("result") or [] if isinstance(w, dict)]
    if items:
        toks = [str(w.get("word", "")).lower() for w in items]
        confs = [float(w.get("conf", 1.0)) for w in items]
    else:
        toks = str(result.get("text", "")).lower().split()
        confs = [1.0] * len(toks)
    ph, n = tuple(words), len(words)
    for i in range(len(toks) - n + 1):
        if n and tuple(toks[i:i + n]) == ph and min(confs[i:i + n]) >= min_confidence:
            after = len(toks) - i - n
            try:
                return float(items[i + n - 1]["end"]), after
            except (IndexError, KeyError, TypeError, ValueError):
                return None, after
    return None, 0


class VoskKeywordRecognizer:
    """Vosk ``KaldiRecognizer`` restricted to a grammar (wake phrases + distractors + [unk])."""

    def __init__(self, model: Any, grammar: list[str], sample_rate: int = 16000) -> None:
        import vosk  # noqa: PLC0415 - optional dependency

        self.sample_rate = int(sample_rate)
        self._model = model  # keeps the shared model alive as long as this recogniser
        self._rec = vosk.KaldiRecognizer(model, float(self.sample_rate), json.dumps(grammar))
        self._rec.SetWords(True)

    def accept(self, pcm16: bytes) -> dict[str, Any] | None:
        if not self._rec.AcceptWaveform(pcm16):
            return None
        try:
            out = json.loads(self._rec.Result() or "{}")
        except ValueError:
            return None
        return out if isinstance(out, dict) else None

    def reset(self) -> None:
        self._rec.Reset()

    def partial(self) -> str:
        try:
            out = json.loads(self._rec.PartialResult() or "{}")
        except ValueError:
            return ""
        return str(out.get("partial") or "") if isinstance(out, dict) else ""


class WakeWordService(Service):
    """``ctx.wake``: listens for the wake phrases in a background thread and starts live voice."""

    name = "wake"

    def __init__(self, ctx: Any, *, recognizer_factory: Callable[[int], KeywordRecognizer] | None = None,
                 allow_simulated_mic: bool = False) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.wake
        self._factory = recognizer_factory  # None: Vosk
        self._allow_sim = allow_simulated_mic
        self.model_path: Path | None = resolve_model_path(self.cfg.model_path)
        self._sample_rate = int(ctx.config.audio.microphone.sample_rate or 16000)
        _, self._phrases, _ = build_grammar(self.cfg.phrases, distractors=False)
        self._video_phrases = self._phrases_for_videos()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Subscription] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # _cv guards the microphone hand-over (_holding / _audio_busy) and wakes the thread up.
        self._cv = threading.Condition()
        self._holding = False  # the listener thread has the microphone stream open
        self._audio_busy = False  # the AudioService needs the microphone (release_mic .. mic_free)
        self._busy_reason = ""
        self._triggered = False  # a wake phrase is being handled on the event loop
        self._cooldown_until = 0.0
        self._retry_at = 0.0
        self._mic_failures = 0  # in a row (only the first one is logged as a warning)
        self._recognizer: KeywordRecognizer | None = None
        self._fed = 0  # samples given to this recogniser so far: its word times count from its first one
        self._unavailable: tuple[str, str] | None = None  # (code, detail) while it can't work
        self._next_check = 0.0
        self._paused = "starting"
        self._listening = False
        self._listened_once = False
        self._relisten_note = False  # log "listening again" once after a wake phrase was handled
        self._gain = 1.0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._attached: Any = None
        self.triggers = 0
        self.last_heard: dict[str, Any] | None = None
        self.video_ignored = 0  # phrases heard while a video played that didn't count (stricter rules)
        self.ducks = 0
        self._duck_until = 0.0
        self._last_partial = ""

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._paused = "starting"
        bus = self.ctx.bus
        if not self._subs:
            self._subs = [
                bus.subscribe(Topics.SETTINGS_CHANGED, self._on_setting),
                bus.subscribe(Topics.PRIVACY_CHANGED, lambda _e: self._poke()),
                bus.subscribe(Topics.STATE_CHANGED, lambda _e: self._poke()),
            ]
        audio = self.ctx.audio
        if audio is not None and hasattr(audio, "attach_background_listener"):
            audio.attach_background_listener(self)
            self._attached = audio
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._main, name="littlebot-wake", daemon=True)
            self._thread.start()
        self._update_health()

    async def stop(self) -> None:
        self._stop.set()
        self._poke()
        thread = self._thread
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 3.0)
        self._thread = None
        for t in list(self._tasks):
            t.cancel()
        if self._attached is not None:
            with contextlib.suppress(Exception):
                self._attached.detach_background_listener(self)
            self._attached = None
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        self._recognizer = None
        self._paused, self._listening = "stopped", False
        self.set_status(ServiceStatus.STOPPED)

    def _on_setting(self, event: Event) -> None:
        if event.get("key") == "wake.enabled":
            log.info("wake word turned %s", "on" if event.get("value") else "off")
            self._poke()

    def _poke(self) -> None:
        with self._cv:
            self._cv.notify_all()

    # ------------------------------------------------- AudioService hand-over
    def release_mic(self, reason: str) -> None:
        """The AudioService needs the microphone: stop reading and close the stream (blocking)."""
        with self._cv:
            self._audio_busy, self._busy_reason = True, reason
            self._cv.notify_all()
            deadline = time.monotonic() + RELEASE_TIMEOUT
            while self._holding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("wake word: microphone not released within %.1fs (%s)", RELEASE_TIMEOUT, reason)
                    break
                self._cv.wait(remaining)

    def mic_free(self) -> None:
        with self._cv:
            self._audio_busy, self._busy_reason = False, ""
            self._cv.notify_all()

    # ------------------------------------------------------------ gates
    def enabled(self) -> bool:
        try:
            return bool(self.ctx.settings.get("wake.enabled", self.cfg.enabled))
        except Exception:  # noqa: BLE001
            return False

    def _check_available(self) -> tuple[str, str] | None:
        """(code, detail) when the wake word can't work on this robot, else None (cached)."""
        now = time.monotonic()
        if self._recognizer is not None or now < self._next_check:
            return self._unavailable if self._recognizer is None else None
        self._next_check = now + AVAILABILITY_RECHECK
        audio = self.ctx.audio
        if audio is None or not hasattr(audio, "background_input"):
            self._unavailable = ("no_audio", PAUSE_TEXT["no_audio"])
        elif self._factory is not None:
            self._unavailable = None
        elif not vosk_installed():
            self._unavailable = ("not_installed", "The offline speech recogniser (vosk) isn't installed: re-run "
                                 "scripts/install_pi.sh, or run .venv/bin/python -m pip install -e '.[local-stt]' "
                                 "and restart LittleBot.")
        elif not model_present(self.model_path):
            self._unavailable = ("model_missing", f"The wake-word model is missing ({self._model_label()}): run "
                                 ".venv/bin/python scripts/download_models.py vosk-en (40 MB). LittleBot picks it "
                                 "up within a few seconds.")
        else:
            self._unavailable = None  # (also after a failed load: _get_recognizer tries again)
        return self._unavailable

    def _model_label(self) -> str:
        p = self.model_path
        if p is None:
            return "wake.model_path is not set"
        with contextlib.suppress(ValueError):
            return str(p.relative_to(PROJECT_ROOT))
        return str(p)

    def _live_active(self) -> bool:
        live = self.ctx.live
        if live is None:
            return False
        try:
            return bool(live.status().get("active"))
        except Exception:  # noqa: BLE001
            return False

    def _pause_reason(self) -> str | None:
        """Why the listener must not listen right now (a PAUSE_TEXT code), or None."""
        ctx = self.ctx
        if self._stop.is_set():
            return "stopped"
        unavailable = self._check_available() is not None  # also while off: the page can say what's missing
        if not self.enabled():
            return "disabled"
        if unavailable:
            return "unavailable"
        try:
            if not ctx.privacy.microphone_enabled:
                return "microphone_off"
        except Exception:  # noqa: BLE001 - fail closed
            return "microphone_off"
        if self._triggered:
            return "starting_live"
        now = time.monotonic()
        if now < self._cooldown_until:
            return "cooldown"
        if now < self._retry_at:
            return "mic_error"
        if self._audio_busy:
            return "mic_in_use"
        if self._live_active():
            return "live_voice"
        if not self.cfg.during_videos and self._video_on():
            return "video"
        return None

    def _video_on(self) -> bool:
        try:
            return self.ctx.state.state == RobotState.PLAYING_MEDIA
        except Exception:  # noqa: BLE001
            return False

    def _phrases_for_videos(self) -> list[tuple[str, tuple[str, ...]]]:
        """The wake phrases that count while a video plays (``wake.video_phrases``; [] = all of them)."""
        _, wanted, _ = build_grammar(self.cfg.video_phrases, distractors=False)
        names = {p for p, _ in wanted}
        return [p for p in self._phrases if p[0] in names] if names else list(self._phrases)

    # ------------------------------------------------------------ the thread
    def _main(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._pause_reason()
                if reason is None:
                    rec = self._get_recognizer()
                    if rec is None:
                        reason = "unavailable"
                    else:
                        inp, reason = self._acquire()
                        if inp is not None:
                            started = time.monotonic()
                            try:
                                detected = self._listen(inp, rec)
                            finally:
                                self._release()
                            if not detected and time.monotonic() - started < MIN_LISTEN_SECONDS:
                                self._stop.wait(MIN_LISTEN_SECONDS)
                            continue
                self._set_paused(reason or "mic_in_use")
                if reason in _LONG_PAUSES:
                    self._recognizer = None  # frees the model unless the local STT also uses it
                with self._cv:
                    if not self._stop.is_set():
                        self._cv.wait(GATE_POLL_SECONDS)
            except Exception:  # noqa: BLE001 - the listener must survive anything
                log.exception("wake word listener failed; retrying in %.0fs", RETRY_SECONDS)
                self._listening = False
                self._stop.wait(RETRY_SECONDS)
        self._listening = False

    def _get_recognizer(self) -> KeywordRecognizer | None:
        if self._recognizer is not None:
            return self._recognizer
        try:
            if self._factory is not None:
                rec = self._factory(self._sample_rate)
            else:
                rec = self._vosk_recognizer()
        except Exception as e:  # noqa: BLE001 - unreadable model, missing library ...
            log.warning("wake word: could not load the speech model: %s", e)
            self._unavailable = ("load_failed", f"The wake-word model could not be loaded ({e}).")
            self._next_check = time.monotonic() + AVAILABILITY_RECHECK * 6
            return None
        if rec is None:
            return None
        self._recognizer, self._unavailable, self._fed = rec, None, 0
        return rec

    def _vosk_recognizer(self) -> KeywordRecognizer | None:
        assert self.model_path is not None
        t0 = time.monotonic()
        model = load_model(self.model_path)
        known = None
        if hasattr(model, "vosk_model_find_word"):
            known = lambda w: model.vosk_model_find_word(w) >= 0  # noqa: E731
        grammar, usable, dropped = build_grammar(self.cfg.phrases, known=known, distractors=self.cfg.distractors)
        if dropped:
            log.warning("wake word: ignoring phrase(s) with words the model doesn't know: %s", ", ".join(dropped))
        if not usable:
            self._unavailable = ("no_phrases", "None of the wake phrases (wake.phrases) can be recognised with "
                                 "this model.")
            self._next_check = time.monotonic() + AVAILABILITY_RECHECK * 6
            return None
        self._phrases = usable
        self._video_phrases = self._phrases_for_videos()
        if self.cfg.during_videos and not self._video_phrases:
            log.warning("wake word: none of wake.video_phrases is a usable wake phrase; nothing counts during videos")
        rec = VoskKeywordRecognizer(model, grammar, self._sample_rate)
        log.info("wake word: model ready in %.1fs (%d phrases, %d words)", time.monotonic() - t0, len(usable),
                 len(grammar))
        return rec

    def _acquire(self) -> tuple[Any, str | None]:
        """The microphone backend, now marked as held by this thread; or (None, pause reason)."""
        audio = self.ctx.audio
        with self._cv:
            if self._stop.is_set():
                return None, "stopped"
            if self._audio_busy:
                return None, "mic_in_use"
            inp = audio.background_input() if audio is not None else None
            if inp is None:
                return None, "mic_in_use"
            if not getattr(inp, "real", False) and not self._allow_sim:
                return None, "no_microphone"
            self._holding = True
            return inp, None

    def _release(self) -> None:
        with self._cv:
            self._holding = False
            self._cv.notify_all()

    def _set_paused(self, reason: str) -> None:
        if reason != self._paused:
            if reason == "unavailable" and self.enabled():
                detail = (self._unavailable or ("", ""))[1]
                log.info("wake word unavailable: %s", detail)
            else:
                log.debug("wake word paused (%s)", reason)
            self._paused = reason
            self._update_health()

    def _refresh_gain(self) -> None:
        try:
            g = float(getattr(self.ctx.audio, "input_gain", 1.0))
            self._gain = g if 0.0 < g <= 8.0 else 1.0
        except (TypeError, ValueError):
            self._gain = 1.0

    def _muted(self) -> bool:
        audio = self.ctx.audio
        fn = getattr(audio, "mic_muted", None)
        try:
            return bool(fn()) if fn is not None else False
        except Exception:  # noqa: BLE001
            return False

    def _listen(self, inp: Any, rec: KeywordRecognizer) -> bool:
        """Read and recognise until a wake phrase (True) or until something needs the mic (False)."""
        if isinstance(inp, SoundDeviceInput) and inp.status in (dev.STATUS_DISCONNECTED, dev.STATUS_UNAVAILABLE):
            dev.refresh_portaudio()  # find a re-plugged USB microphone (only while no stream is open)
        try:
            inp.open()
        except AudioDeviceError as e:
            self._mic_failed(f"could not open the microphone: {e}")
            return False
        if self._mic_failures:
            log.info("wake word: the microphone works again (after %d %s)", self._mic_failures,
                     "try" if self._mic_failures == 1 else "tries")
        self._mic_failures = 0
        rec.reset()
        self._refresh_gain()
        self._listening, self._paused = True, ""
        if not self._listened_once:
            self._listened_once = True
            log.info("wake word: listening for %s", " / ".join(f"'{p}'" for p, _ in self._phrases))
        elif self._relisten_note:
            self._relisten_note = False
            log.info("wake word: listening again")
        else:
            log.debug("wake word: listening")
        self._update_health()
        pending: list[np.ndarray] = []
        pending_n = 0
        muted = False
        self._last_partial = ""
        heard = PreRoll(rec.sample_rate, PREROLL_MAX_SECONDS)  # the sentence being recognised (see _carry_from)
        next_gate = time.monotonic() + GATE_POLL_SECONDS
        try:
            while not self._stop.is_set() and not self._audio_busy:
                now = time.monotonic()
                if now >= next_gate:
                    if self._pause_reason() is not None:
                        return False
                    self._refresh_gain()
                    next_gate = now + GATE_POLL_SECONDS
                try:
                    chunk = inp.read(READ_SECONDS)
                except AudioDeviceError as e:
                    self._mic_failed(f"the microphone stopped: {e}")
                    return False
                if chunk is None or not len(chunk):
                    if not inp.is_open:
                        return False  # closed under us (privacy switch, device gone)
                    continue
                if self._muted():  # LittleBot is making sound: never listen to itself
                    if not muted:
                        rec.reset()
                        pending, pending_n, muted = [], 0, True
                        heard.clear()
                    continue
                muted = False
                pending.append(chunk)
                pending_n += len(chunk)
                if pending_n < FEED_SAMPLES:
                    continue
                block = np.concatenate(pending) if len(pending) > 1 else pending[0]
                pending, pending_n = [], 0
                if self._gain != 1.0:
                    block = np.clip(block * self._gain, -1.0, 1.0)
                if inp.sample_rate != rec.sample_rate:
                    block = dsp.resample(block, inp.sample_rate, rec.sample_rate)
                heard.add(self._fed, block)
                result = rec.accept(dsp.to_int16(block).tobytes())
                self._fed += len(block)
                del block
                video = self._video_on()
                if not result:
                    if video and self.cfg.video_duck:
                        self._maybe_duck(rec)
                    continue
                self._last_partial = ""
                phrase = self._match(result, video)
                if phrase is not None and not self._audio_busy and not self._stop.is_set():
                    # Not during a video: its soundtrack reaches the microphone and would be sent as their words.
                    carry = None if video else self._carry_from(heard, result, phrase, rec.sample_rate)
                    self._detected(phrase, carry)
                    if carry is not None:
                        self._carry_on(inp, rec.sample_rate, carry)
                    return True
                heard.decided()  # no wake phrase in that sentence: it is forgotten
            return False
        finally:
            self._listening = False
            pending.clear()
            heard.clear()
            with contextlib.suppress(Exception):
                inp.close()

    def _match(self, result: dict[str, Any], video: bool) -> str | None:
        """The wake phrase in a final result; while a video plays only the video phrases, more confident."""
        if not video:
            return match_wake_phrase(result, self._phrases, self.cfg.min_confidence)
        phrase = match_wake_phrase(result, self._video_phrases, self.cfg.video_min_confidence)
        if phrase is None:
            heard = match_wake_phrase(result, self._phrases, 0.0)
            if heard is not None:  # something like it, from the video or from a person
                self.video_ignored += 1
                why = ("only " + " / ".join(f"'{p}'" for p, _ in self._video_phrases) + " counts then"
                       if not any(p == heard for p, _ in self._video_phrases)
                       else f"word confidence below wake.video_min_confidence {self.cfg.video_min_confidence:g}")
                log.info("wake word: '%s' ignored while a video plays (%s)", heard, why)
        return phrase

    def _maybe_duck(self, rec: KeywordRecognizer) -> None:
        """``wake.video_duck``: the words so far end in "hey": turn the video down while the rest is said."""
        partial = getattr(rec, "partial", None)
        if partial is None:
            return
        try:
            text = str(partial() or "")
        except Exception:  # noqa: BLE001
            return
        if text == self._last_partial:
            return
        self._last_partial = text
        words = text.split()
        leads = {ph[0] for _, ph in self._video_phrases}
        now = time.monotonic()
        if not words or words[-1] not in leads or now < self._duck_until:
            return
        media = self.ctx.media
        secs = float(self.cfg.video_duck_seconds)
        try:
            ok = bool(media is not None and media.duck(float(self.cfg.video_duck_volume), secs))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._duck_until = now + secs
            self.ducks += 1
            log.debug("wake word: heard '%s' during a video; turned it down for %.1f s", words[-1], secs)

    def _quiet_level(self) -> float:
        try:
            return float(self.ctx.config.audio.microphone.vad_threshold) * QUIET_RATIO
        except (AttributeError, TypeError, ValueError):
            return 0.015 * QUIET_RATIO

    def _carry_from(self, heard: PreRoll, result: dict[str, Any], phrase: str, rate: int) -> SpeechCarry:
        """What was said with the wake phrase, for the call: the sentence after it (by the recogniser's word
        times; the whole sentence when there are none)."""
        words = dict(self._phrases).get(phrase) or tuple(phrase.split())
        end, after = wake_phrase_end(result, words, self.cfg.min_confidence)
        cut = None if end is None else max(0, int((end - WAKE_CUT_MARGIN) * rate))
        carry = SpeechCarry(rate, phrase=phrase, quiet_level=self._quiet_level(), kept_talking=after > 0)
        for block in heard.since(cut):
            carry.add(block)
        heard.clear()
        carry.lead_s = carry.seconds
        return carry

    def _carry_on(self, inp: Any, rate: int, carry: SpeechCarry) -> None:
        """After the wake phrase: keep what they say for the call until live voice takes the microphone
        (``release_mic``; while they talk, at their next pause), it can't start (the carry is dropped) or
        CARRY_WAIT_SECONDS. What the microphone hears while LittleBot makes a sound (the earcon) is left out."""
        deadline = time.monotonic() + CARRY_WAIT_SECONDS
        why = ""
        asked: float | None = None  # when live voice asked for the microphone
        when = ""
        muted = False
        try:
            while carry.is_open:
                now = time.monotonic()
                if self._audio_busy:  # hand it over at a pause (or while LittleBot's own sound is left out anyway)
                    asked = now if asked is None else asked
                    if muted or carry.quiet_s >= HANDOVER_QUIET_SECONDS:
                        if now - asked >= 0.05:
                            when = f" at a pause ({int((now - asked) * 1000)} ms after it asked)"
                        break
                    if now - asked >= HANDOVER_WAIT_SECONDS:
                        when = f" while they were still talking (no pause within {HANDOVER_WAIT_SECONDS:g} s)"
                        break
                if self._stop.is_set() or not self._mic_allowed():
                    carry.drop("stopped" if self._stop.is_set() else "microphone off")
                    return
                if now >= deadline:
                    why = f"live voice didn't take the microphone within {CARRY_WAIT_SECONDS:g} s"
                    break
                try:
                    chunk = inp.read(READ_SECONDS)
                except AudioDeviceError as e:
                    self._mic_failed(f"the microphone stopped: {e}")
                    why = "the microphone stopped"
                    break
                if chunk is None or not len(chunk):
                    if not inp.is_open:
                        why = "the microphone was closed"
                        break
                    continue
                muted = self._muted()
                if muted:  # LittleBot's own sound (the earcon) is never carried
                    continue
                if self._gain != 1.0:
                    chunk = np.clip(chunk * self._gain, -1.0, 1.0)
                if inp.sample_rate != rate:
                    chunk = dsp.resample(chunk, inp.sample_rate, rate)
                carry.add(chunk)
        finally:
            done = time.monotonic()
            if not why:
                busy = self._busy_reason
                why = ("live voice took the microphone" if busy in ("", "lent")
                       else f"the microphone was needed ({busy})") + when
            carry.seal(why)
            if not carry.dropped:
                log.info("wake word: kept %.1f s of what they said for live voice (%.1f s of it speech, %.1f s "
                         "said with the wake phrase); %s %d ms after the wake phrase", carry.seconds,
                         carry.speech_seconds, carry.lead_s, why, int((done - carry.heard_at) * 1000))

    def _mic_allowed(self) -> bool:
        try:
            return bool(self.ctx.privacy.microphone_enabled)
        except Exception:  # noqa: BLE001 - fail closed
            return False

    def _mic_failed(self, why: str) -> None:
        self._mic_failures += 1
        (log.warning if self._mic_failures == 1 else log.debug)("wake word: %s; retrying every %.0fs", why,
                                                                RETRY_SECONDS)
        self._retry_at = time.monotonic() + RETRY_SECONDS
        audio = self.ctx.audio
        with contextlib.suppress(Exception):
            audio._publish_device("microphone")  # noqa: SLF001 - keep the dashboard's device status fresh

    # ------------------------------------------------------------ detection
    def _detected(self, phrase: str, carry: SpeechCarry | None = None) -> None:
        """Runs in the listener thread (which then keeps ``carry`` filled until live voice takes the
        microphone): hand over to the loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            if carry is not None:
                carry.drop("no event loop")
            return
        with self._cv:
            self._triggered = True
        self.triggers += 1
        self.last_heard = {"phrase": phrase, "at": time.time(), "result": "starting", "detail": None}
        log.info("wake word heard (phrase=%s)", phrase)
        try:
            asyncio.run_coroutine_threadsafe(self._handle(phrase, carry), loop)
        except RuntimeError:  # loop closed meanwhile
            if carry is not None:
                carry.drop("no event loop")
            with self._cv:
                self._triggered = False

    async def _handle(self, phrase: str, carry: SpeechCarry | None = None) -> None:
        ctx = self.ctx
        entered = False
        video: str | None = None
        started = False
        try:
            with contextlib.suppress(Exception):
                ctx.bus.publish(WAKE_TOPIC, {"phrase": phrase}, source="wake")
            video = await self._pause_video()  # first: the earcon is then heard over a quiet speaker
            with contextlib.suppress(Exception):
                entered = ctx.state.state != RobotState.LISTENING and ctx.state.transition(RobotState.LISTENING,
                                                                                          "wake word")
            if carry is not None and carry.kept_talking:
                # They asked in the same breath and wait for the answer, not for a sound; a sound now would also
                # hide what they say next (half-duplex). The face shows it is listening.
                log.info("wake word: no earcon, they kept talking (%.1f s said after the wake phrase)", carry.lead_s)
            else:
                await self._play(self.cfg.sound)
            live = ctx.live
            if live is None:
                res: dict[str, Any] = {"ok": False, "error": "unavailable",
                                       "detail": "Live voice isn't available on this robot."}
            else:
                log.info("wake word: starting live voice")
                res = await live.start_session(parent_present=True, carry=carry)
            if res.get("ok"):
                started = True  # the call has the carry now (it drops it when it ends)
                log.info("wake word: live voice started; connecting")
                self._note("started")
                self._spawn(self._watch_connect(live))
                if video is not None or self._video_held():
                    self._spawn(self._after_video_chat(live, paused_now=video is not None))
                return
            error, detail = str(res.get("error") or "failed"), str(res.get("detail") or "")
            if carry is not None:
                carry.drop("live voice could not start")  # what they said is forgotten, never sent
            log.warning("wake word: live voice could not start (%s%s)", error, f": {detail}" if detail else "")
            self._note("blocked", error=error, detail=detail)
            if entered:
                ctx.state.release(RobotState.LISTENING, "wake word: live voice could not start")
            await self._play("uhoh", "error sound")
            if video is not None:
                await self._resume_video("live voice could not start")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("wake word: starting live voice failed")
            if carry is not None and not started:
                carry.drop("live voice failed")
            self._note("failed", error=type(e).__name__, detail=str(e))
            if entered:
                with contextlib.suppress(Exception):
                    ctx.state.release(RobotState.LISTENING, "wake word: live voice failed")
            await self._play("uhoh", "error sound")
            if video is not None:
                await self._resume_video("live voice failed")
        finally:
            if carry is not None and not started:
                carry.drop("live voice did not start")
            with self._cv:
                self._triggered = False
                self._relisten_note = True
                self._cooldown_until = time.monotonic() + float(self.cfg.cooldown_seconds)
                self._cv.notify_all()

    async def _watch_connect(self, live: Any) -> None:
        """Live voice connects in the background: an "uhoh" if that fails, so the child knows."""
        timeout = float(getattr(self.ctx.config.chatgpt, "live_connect_timeout_seconds", 20.0)) + CONNECT_GRACE_SECONDS
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                st = live.status()
            except Exception:  # noqa: BLE001
                return
            if st.get("active"):
                if st.get("phase") not in (None, "connecting"):
                    ms = st.get("connect_ms")
                    log.info("wake word: live voice connected%s", f" in {ms} ms" if ms is not None else "")
                    self._note("connected")
                    return
                continue
            last = st.get("last_call") or {}
            if not last.get("connected"):
                why = last.get("error") or last.get("ended_reason") or "ended before connecting"
                log.warning("wake word: live voice did not connect (%s)", why)
                self._note("failed", error="not_connected", detail=str(why))
                await self._play("uhoh", "error sound")
            return
        log.warning("wake word: live voice still connecting after %.0f s", timeout)

    # ------------------------------------------------------------ a video playing
    async def _pause_video(self) -> str | None:
        """A video is playing: pause it for the chat (position kept, the face back). Its title, else None."""
        media = self.ctx.media
        if media is None or not hasattr(media, "hold") or not self._video_on():
            return None
        try:
            st = media.status()
            if st.get("held") or st.get("state") not in ("loading", "playing", "paused"):
                return None
            ok = await media.hold("wake word", wait=float(self.cfg.video_pause_wait_seconds))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - live voice still starts (it then stops the video)
            log.exception("wake word: pausing the video failed")
            return None
        if not ok:
            return None
        title = str((st.get("item") or {}).get("title") or "the video")
        log.info("wake word: the video is paused for the chat")
        heard = self.last_heard
        if heard is not None:
            heard["video"] = title
        return title

    async def _after_video_chat(self, live: Any, *, paused_now: bool = True) -> None:
        """A chat while a video is paused for one. Asked to keep watching: it carries on (also when the call
        ended before the voice's goodbye). For the chat that paused it (``paused_now``): nobody said anything in
        it (it didn't connect, or most likely the wake word heard the video itself, ``wake.video_no_words_seconds``)
        also carries on with it. Otherwise it waits (live voice handles "keep watching" / "stop the video") and
        stops ``media.chat_pause_minutes`` after the chat."""
        no_words = float(self.cfg.video_no_words_seconds) if paused_now else 0.0
        connected_at: float | None = None
        while True:
            await asyncio.sleep(0.25)
            try:
                st = live.status()
            except Exception:  # noqa: BLE001
                return
            if not st.get("active"):
                break
            if st.get("phase") in (None, "connecting"):
                continue
            connected_at = connected_at or time.monotonic()
            if no_words > 0 and st.get("heard_user") is False and time.monotonic() - connected_at >= no_words:
                log.info("wake word: nobody spoke within %.0f s of the call that paused the video; ending it",
                         no_words)
                await live.stop_session(NO_WORDS_REASON)
                break
        try:
            last = live.status().get("last_call") or {}
        except Exception:  # noqa: BLE001
            return
        if last.get("video") == "resume":  # asked for in the chat (also if it ended before the voice's goodbye)
            await self._resume_video("they asked to keep watching")
        elif paused_now and (last.get("heard_user") is False or last.get("connected") is False):
            await self._resume_video("nobody spoke in the chat" if last.get("connected") is not False
                                     else "live voice did not connect")

    def _video_held(self) -> bool:
        media = self.ctx.media
        try:
            return bool(media is not None and (media.status() or {}).get("held"))
        except Exception:  # noqa: BLE001
            return False

    async def _resume_video(self, why: str) -> None:
        """Carry on with a video paused for a chat (if it still is)."""
        media = self.ctx.media
        try:
            if media is None or not (media.status() or {}).get("held"):
                return
            ok = await media.resume()
            if not ok and not (media.status() or {}).get("held"):
                return  # carried on (the voice's hand-over) or stopped meanwhile
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("wake word: carrying on with the video failed")
            return
        log.info("wake word: %s; the video %s", why, "carries on" if ok else "couldn't carry on (it stays paused)")

    async def _play(self, name: str, what: str = "earcon") -> bool:
        """Play ``name`` and log whether it was heard (and if not, why). Never raises, never hangs."""
        audio = self.ctx.audio
        if not name:
            return False
        if audio is None:
            log.warning("wake word: %s '%s' not played (no audio system)", what, name)
            return False
        try:
            ok = bool(await asyncio.wait_for(audio.play_sound(name), EARCON_TIMEOUT))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("wake word: %s '%s' not played within %.0f s (the speaker seems stuck); going on",
                        what, name, EARCON_TIMEOUT)
            return False
        except Exception as e:  # noqa: BLE001 - a missing sound never blocks live voice
            log.warning("wake word: %s '%s' not played (%s: %s)", what, name, type(e).__name__, e)
            return False
        if ok:
            log.info("wake word: %s '%s' played (%s)", what, name, _speaker_note(audio))
        else:
            lent = getattr(audio, "suspended", None)
            why = f"the speaker is lent to {lent}" if lent else f"{_speaker_note(audio)}; see the audio log"
            log.warning("wake word: %s '%s' not played (%s)", what, name, why)
        return ok

    def _note(self, result: str, *, error: str | None = None, detail: str | None = None) -> None:
        heard = dict(self.last_heard or {"phrase": None, "at": time.time()})
        heard.update(result=result, error=error, detail=detail)
        self.last_heard = heard

    def _spawn(self, coro: Any) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ status
    def _state(self) -> str:
        if self._listening:
            return "listening"
        if self._paused == "stopped":
            return "stopped"
        if not self.enabled():
            return "disabled"
        if self._paused == "unavailable" or (self._recognizer is None and self._unavailable is not None):
            return "unavailable"
        return "paused"

    def _update_health(self) -> None:
        if self._paused == "stopped":
            return
        state = self._state()
        if state == "unavailable":
            self.set_status(ServiceStatus.DEGRADED, (self._unavailable or ("", "unavailable"))[1])
        elif state == "disabled":
            self.set_status(ServiceStatus.DISABLED, "turned off (wake.enabled)")
        else:
            self.set_status(ServiceStatus.RUNNING, "")

    def status(self) -> dict[str, Any]:
        state = self._state()
        unavailable = self._unavailable if self._recognizer is None else None
        video = state == "listening" and self.cfg.during_videos and self._video_on()
        if video:
            detail = ("Listening for " + " or ".join(f"“{p}”" for p, _ in self._video_phrases)
                      + " (a video is playing).")
        elif state == "listening":
            detail = "Listening for " + " or ".join(f"“{p}”" for p, _ in self._phrases) + "."
        elif state == "unavailable":
            detail = (unavailable or ("", "Not available."))[1]
        elif state == "disabled":
            detail = PAUSE_TEXT["disabled"]
        else:
            detail = PAUSE_TEXT.get(self._paused, "Paused.")
        return {
            "enabled": self.enabled(),
            "available": unavailable is None,
            "listening": state == "listening",
            "state": state,
            "paused": None if state == "listening" else (self._paused or None),
            "detail": detail,
            "unavailable": unavailable[0] if unavailable else None,
            "unavailable_detail": unavailable[1] if unavailable else None,
            "model_missing": bool(unavailable and unavailable[0] == "model_missing"),
            "model": self._model_label() if self._factory is None else "custom",
            "phrases": [p for p, _ in self._phrases],
            "during_video": video,
            "video_phrases": [p for p, _ in self._video_phrases] if self.cfg.during_videos else [],
            "video_ignored": self.video_ignored,
            "ducks": self.ducks,
            "triggers": self.triggers,
            "last_heard": dict(self.last_heard) if self.last_heard else None,
        }


def _speaker_note(audio: Any) -> str:
    """The speaker as the log should show it: device, volume and anything that keeps it quiet."""
    try:
        st = audio.status()
        spk = st.get("speaker") or {}
        vol = float(st.get("volume", 0.0))
    except Exception:  # noqa: BLE001 - only for the log
        return "speaker status unknown"
    note = f"{spk.get('device') or spk.get('backend') or 'speaker'}, volume {vol:.0%}"
    if spk.get("status") not in (None, dev.STATUS_OK):
        note += f", speaker {spk.get('status')}: {spk.get('detail') or ''}".rstrip(": ")
    if vol <= 0.01:
        note += ": the volume is 0, so nothing was heard"
    return note


def create_service(ctx: Any) -> WakeWordService:
    return WakeWordService(ctx)


__all__ = ["WakeWordService", "VoskKeywordRecognizer", "build_grammar", "match_wake_phrase", "normalise_phrase",
           "create_service", "DISTRACTOR_WORDS"]
