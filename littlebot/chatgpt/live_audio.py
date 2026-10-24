"""Microphone and speaker for a live call: 20 ms frames in, the model's voice out.

Half-duplex gate: while LittleBot's speaker is playing the model's voice (and for ``echo_tail``
after), the microphone sends silence, or the model would hear itself and answer itself. "Speaking"
is measured on the audio actually handed to the speaker, so the gate follows what is audible, not
what arrived over the network.

Barge-in (:class:`BargeInDetector`): with PipeWire's echo cancelling (scripts/setup_echo_cancel.sh,
the ``littlebot_ec_*`` devices) a grown-up can talk over the voice. While the voice plays, the
microphone still sends silence, but the detector watches the echo-cancelled microphone for a
grown-up's voiced speech, louder than what is left of LittleBot's own voice. When it hears one
(:meth:`LiveAudio._interrupt`) the voice fades out at once and is *held*, and the microphone is
sent from their first word (a short ring buffer of what the gate kept back). ChatGPT's own turn
detection then takes their turn: once their words arrive (:meth:`LiveAudio.barge_in_heard`) the
held voice is dropped, and so is the rest of the turn they cut into, until the next one starts.
No words within ``false_alarm_s`` (a cough, the baby): the held voice plays on. Without echo
cancelling (``chatgpt.live_barge_in: auto``) it stays plain half-duplex.

The face's mouth follows the voice too: every block handed to the speaker gets a level and a
mouth shape (:class:`~littlebot.audio.dsp.VisemeEstimator`), stamped with the moment PortAudio
says it will be heard, and :attr:`LiveAudio.mouth` reads the one being heard now.

A call started by the wake word sends what was said with it first (:class:`CarryFeed`).

A USB microphone or speaker can stop mid-call (a power dip resets it, or another program still had
it): :meth:`LiveAudio.stalled` says which one went quiet, and :meth:`LiveAudio.reopen` opens both
again (littlebot/chatgpt/live.py does this, and ends the call if it doesn't help).

Nothing is recorded: samples only pass through small in-memory buffers.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
import math
import re
import threading
import time
from typing import Any, Callable

import numpy as np

from littlebot.audio import devices as dev
from littlebot.audio import dsp
from littlebot.audio.errors import AudioDeviceError

log = logging.getLogger("littlebot.chatgpt.live")

RATE = 48000  # WebRTC / Opus
FRAME = RATE // 50  # 20 ms
MAX_QUEUED_S = 1.0  # more than this waiting for the speaker = we fell behind: drop the oldest
MOUTH_LEAD_S = 0.06  # report the mouth this early: polling (~20 Hz) and drawing the face take about that
MAX_OUTPUT_DELAY_S = 0.5  # a reported speaker delay above this is nonsense: ignore it
# CarryFeed: while frames wait to be sent, each pause is cut to KEEP_PAUSE_S (enough to keep words apart);
# the call is then back in real time at their first pause.
KEEP_PAUSE_S = 0.3
BEHIND_FRAMES = 2  # this many frames waiting = behind real time (fewer is ordinary jitter)
MAX_WAITING_S = 10.0  # never more than this waiting to be sent: the oldest goes
PACE_SLACK_S = 0.06  # a frame this late (the microphone or the encoder was slow) restarts the beat: no burst
STALL_S = 2.0  # no block from the microphone (or no request from the speaker) for this long: that stream stopped
OPEN_RETRY_S = 3.0  # a device still busy (a video's browser was just closed): keep trying this long
OPEN_RETRY_STEP_S = 0.25
REOPEN_S = 8.0  # reopen(): a device that went away mid-call is looked for this long
REOPEN_STEP_S = 0.5
# --- barge-in (BargeInDetector, LiveAudio._barge_frame); the main thresholds are BargeInSettings / config ---
FRAME_S = FRAME / RATE
PITCH_RATE = 8000  # the detector looks for a pitch in the microphone at this rate (48 kHz / 6)
ADULT_MIN_HZ = 75.0  # grown-ups' voices: about 80-260 Hz; a baby's babble 300-600 Hz
PITCH_MAX_HZ = 650.0
VOICED_R = 0.5  # normalised autocorrelation at the pitch: a voiced (pitched) sound
ADULT_SHARE = 0.4  # of a run's frames, at least this share voiced at a grown-up's pitch
GAP_S = 0.12  # under the threshold this long: their speech (the run) ended; shorter gaps are consonants
WARMUP_S = 0.3  # the voice's echo is measured this long in a call before anything can interrupt
INITIAL_COUPLING = 1.0  # what is left of the voice at the microphone, relative to the speaker, before measuring
FLOOR_OVER = 3.16  # louder than the room's own noise by this much (10 dB) too
ECHO_REF_S = 0.3  # the echo of what the speaker played up to this long ago may still be at the microphone
ECHO_LEARN_MIN = 0.002  # the echo is measured only while the speaker plays at least this loud (RMS after volume)
ID_AFTER_S = 0.2  # ask voice recognition (ctx.voices) about a run once this much was heard
ID_TIMEOUT_S = 1.0
FAST_SPEECH_S = 0.2  # voice recognition says a grown-up: this much speech is enough
PREROLL_MAX_S = 0.8  # the microphone kept for the pre-roll (in memory, the last 0.8 s only)
PREROLL_LEAD_S = 0.1  # the pre-roll starts this long before their speech did
QUIET_LEVEL = 0.0105  # RMS below which a frame of the pre-roll is a pause (as in littlebot/audio/carry.py)
FADE_S = 0.05  # the voice fades out (and back in) over this long
HOLD_MAX_S = 4.0  # the voice is held (not dropped) up to this long while deciding (FALSE_ALARM_MAX_S)
STILL_TALKING_S = 0.3  # loud this recently: they are still talking (a false alarm waits for them)
FALSE_ALARM_MAX_S = 4.0  # but no longer than this
DROP_GAP_S = 0.25  # the turn they cut into has ended: the voice was quiet this long ...
USER_DONE_S = 0.25  # ... and they stopped talking this long ago (or their words are final)
DROP_MAX_S = 15.0  # never drop the voice longer than this
# LiveAudio's barge-in states: watching (the gate as usual), held (they cut in: the voice waits, they are heard),
# dropping (ChatGPT heard their words: the rest of the turn they cut into is dropped, they are heard).
_WATCH, _HELD, _DROPPING = "watch", "held", "dropping"


# ---------------------------------------------------------------------------------------------- barge-in
@dataclasses.dataclass
class BargeInSettings:
    """``chatgpt.live_barge_in*`` (littlebot/core/config.py, docs/CHATGPT.md)."""

    mode: str = "auto"  # auto: only with echo cancelling | on | off
    speech_s: float = 0.3  # voiced grown-up speech needed
    over_echo_db: float = 8.0  # louder than what is left of the voice (and the room's noise) by this much
    min_level: float = 0.008  # and at least this loud (RMS, full scale 1.0)
    max_pitch_hz: float = 260.0  # voiced above this: a child (doesn't interrupt)
    false_alarm_s: float = 2.5  # no words from ChatGPT by then: the voice carries on

    @classmethod
    def from_config(cls, cfg: Any) -> BargeInSettings:
        d = cls()
        return cls(mode=str(getattr(cfg, "live_barge_in", d.mode) or d.mode).lower(),
                   speech_s=float(getattr(cfg, "live_barge_in_speech_ms", d.speech_s * 1000)) / 1000,
                   over_echo_db=float(getattr(cfg, "live_barge_in_over_echo_db", d.over_echo_db)),
                   min_level=float(getattr(cfg, "live_barge_in_min_level", d.min_level)),
                   max_pitch_hz=float(getattr(cfg, "live_barge_in_max_pitch_hz", d.max_pitch_hz)),
                   false_alarm_s=float(getattr(cfg, "live_barge_in_false_alarm_ms", d.false_alarm_s * 1000)) / 1000)


def estimate_pitch(x: np.ndarray, rate: int, lo: float = ADULT_MIN_HZ - 5, hi: float = PITCH_MAX_HZ) -> tuple[float, float]:
    """``(F0 in Hz, voicing 0..1)`` of a short mono stretch (40 ms) by normalised autocorrelation; F0 is 0 if none.

    Autocorrelation also peaks at twice the period (half the pitch), so the *shortest* period whose peak is
    nearly as high as the best one wins: a baby's 450 Hz must not read as a grown-up's 225 Hz."""
    n = len(x)
    if n < 16:
        return 0.0, 0.0
    x = x.astype(np.float64) - float(np.mean(x))
    energy = float(np.dot(x, x))
    if energy <= 1e-12:
        return 0.0, 0.0
    lag_min, lag_max = max(2, int(rate / hi)), min(n // 2, math.ceil(rate / lo))
    if lag_max <= lag_min + 2:
        return 0.0, 0.0
    nfft = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(spec.real * spec.real + spec.imag * spec.imag, nfft)[:lag_max + 2]
    lags = np.arange(lag_min - 1, lag_max + 2)
    r = ac[lag_min - 1:lag_max + 2] / energy * (n / (n - lags))  # unbiased: long lags overlap less
    mid = r[1:-1]
    best = float(np.max(mid))
    if best < 0.3:
        return 0.0, max(0.0, best)
    peaks = np.flatnonzero((mid >= r[:-2]) & (mid >= r[2:]) & (mid >= 0.85 * best))
    i = int(peaks[0]) + 1 if len(peaks) else int(np.argmax(mid)) + 1
    a, b, c = r[i - 1], r[i], r[i + 1]
    den = a - 2 * b + c
    shift = 0.5 * (a - c) / den if den < 0 else 0.0  # parabolic interpolation between lags
    lag = lags[i] + max(-0.5, min(0.5, shift))
    return float(rate / lag), float(max(0.0, min(1.0, b)))


@dataclasses.dataclass
class _Run:
    """One stretch of sound loud enough to be someone talking over the voice."""

    onset: float
    last_loud: float
    loud: int = 0
    adult: int = 0
    child: int = 0
    pitches: list[float] = dataclasses.field(default_factory=list)
    grown_up: bool = False  # voice recognition: a grown-up
    vetoed: bool = False  # voice recognition: a child
    voice: str | None = None  # their name, from voice recognition
    asked: bool = False


class BargeInDetector:
    """Is a grown-up talking over LittleBot's voice? Looks at the echo-cancelled microphone 20 ms at a time.

    A frame counts when it is louder (by ``over_echo_db``) than what is left of the voice at the microphone after
    echo cancelling and than the room's own noise, and at least ``min_level``. What is left of the voice is
    measured all the time while only the voice plays: ``coupling`` is the microphone's level over the level the
    speaker played in the last ECHO_REF_S, tracked near the top of its spread (up fast, down slowly) and frozen
    while someone may be talking. Then a run of such frames must go on for ``speech_s`` (gaps of up to GAP_S are
    consonants), with at least ADULT_SHARE of it voiced at a grown-up's pitch (ADULT_MIN_HZ..``max_pitch_hz``)
    and clearly more of it there than above (a child). So a cough (no pitch), a short "mm" (too short), the
    baby (too high), the voice's own echo (not louder than itself) and a quiet TV (the room's noise) don't.

    ``step`` costs one 1024-point FFT pair when a frame is loud, nothing more (measured: docs/CHATGPT.md).
    """

    def __init__(self, settings: BargeInSettings | None = None, rate: int = RATE) -> None:
        self.s = settings or BargeInSettings()
        self.rate = rate
        self._dec = max(1, rate // PITCH_RATE)
        self._over = 10 ** (self.s.over_echo_db / 20)
        self._prev = np.zeros(0, dtype=np.float32)  # the previous frame at PITCH_RATE (pitch over 40 ms)
        self.coupling = INITIAL_COUPLING
        self.floor: float | None = None
        self.voice_s = 0.0  # how long the echo has been measured (WARMUP_S)
        self.threshold = 0.0
        self.level = 0.0
        self.run: _Run | None = None
        self.fired: _Run | None = None  # the run that interrupted last
        self._quiet_after: float = float("-inf")  # no echo measuring until then (someone may still talk)

    def reset_run(self) -> None:
        self.run = None

    def step(self, frame: np.ndarray, echo_ref: float, now: float, *, voice_on: bool) -> bool:
        """One 20 ms frame (int16 or float, 48 kHz). ``echo_ref``: the loudest the speaker played lately (RMS after
        the volume), ``voice_on``: the voice is audible. True once per run, when it decides they are talking."""
        x = frame.astype(np.float32)
        if frame.dtype == np.int16:
            x = x / 32768.0
        level = self.level = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        floor = self.floor if self.floor is not None else 0.0
        threshold = max(self.s.min_level, FLOOR_OVER * floor)
        if voice_on:
            threshold = max(threshold, self._over * self.coupling * echo_ref)
        self.threshold = threshold
        loud = level >= threshold
        if not loud and self.run is None and now >= self._quiet_after:
            self._learn(level, echo_ref, voice_on)
        if not voice_on:  # nothing to talk over: only how loud it is matters (are they still talking?)
            self.run = None
            self._prev = np.zeros(0, dtype=np.float32)
            return False
        n = len(x) // self._dec * self._dec
        low = x[:n].reshape(-1, self._dec).mean(axis=1)
        window = np.concatenate([self._prev, low])
        self._prev = low
        run = self.run
        if loud:
            if run is None:
                run = self.run = _Run(onset=now - FRAME_S, last_loud=now)
            run.last_loud, run.loud = now, run.loud + 1
            self._quiet_after = now + 1.0
            f0, voicing = estimate_pitch(window, PITCH_RATE)
            if voicing >= VOICED_R and f0 > 0:
                if ADULT_MIN_HZ <= f0 <= self.s.max_pitch_hz:
                    run.adult += 1
                    run.pitches.append(f0)
                elif f0 > self.s.max_pitch_hz:
                    run.child += 1
        elif run is not None and now - run.last_loud > GAP_S:
            self.run = None
            self._missed(run)
            return False
        if run is None or run.vetoed or self.voice_s < WARMUP_S:
            return False
        frames = max(1.0, (now - run.onset) / FRAME_S)
        need = FAST_SPEECH_S if run.grown_up else self.s.speech_s
        # only while they are still loud: a short "mm" ends, and the gap after it doesn't count as speech
        if loud and now - run.onset + 1e-6 >= need and run.adult >= ADULT_SHARE * frames and run.adult > 2 * run.child:
            self.fired, self.run = run, None
            return True
        return False

    def _missed(self, run: _Run) -> None:
        """A grown-up-sounding run that ended without interrupting: say why in the log (the baby's babble and
        short noises aren't worth a line)."""
        speech = run.last_loud - run.onset
        if speech < 0.12 or run.adult < 3 or run.adult <= run.child:
            return
        frames = max(1, round(speech / FRAME_S))
        if run.vetoed:
            why = "voice recognition says it was a child"
        elif speech + 1e-6 < (FAST_SPEECH_S if run.grown_up else self.s.speech_s):
            why = f"too short: a pause longer than {GAP_S:.2f} s ended it"
        else:
            why = f"only {run.adult} of {frames} frames voiced at a grown-up's pitch"
        log.info("live voice: someone talked over the voice for %.2f s (pitch %d Hz) but it didn't stop: %s",
                 speech, round(self.pitch_of(run)), why)

    def _learn(self, level: float, echo_ref: float, voice_on: bool) -> None:
        """Measure the echo (while only the voice plays) and the room's noise (while nothing does)."""
        if voice_on and echo_ref >= ECHO_LEARN_MIN:
            ratio = math.log(max(level, 1e-6) / echo_ref)
            c = math.log(self.coupling)
            if self.voice_s < WARMUP_S:
                c += 0.3 * (ratio - c)  # the first measurements: get there quickly
            elif ratio > c:  # near the top of the echo's spread; in small steps, so the soft start of
                c += 0.05 * min(ratio - c, math.log(2.0))  # someone's speech can't hide the rest of it
            else:
                c += 0.01 * (ratio - c)
            self.coupling = math.exp(c)
            self.voice_s += FRAME_S
        elif not voice_on:
            lv = math.log(max(level, 1e-6))
            if self.floor is None:
                self.floor = max(level, 1e-6)
                return
            f = math.log(self.floor)
            f += (0.1 if lv < f else 0.005) * (lv - f)  # the quiet between sounds
            self.floor = math.exp(f)

    @staticmethod
    def pitch_of(run: _Run) -> float:
        return float(np.median(run.pitches)) if run.pitches else 0.0


_GA_LEAD = (r"(?:oops|whoops|oopsie|oh|ah|uh|um|erm|hmm|sorry|so sorry|i'?m sorry|my bad|my mistake|pardon(?: me)?|"
            r"excuse me|no|nope|never ?mind|ok|okay|it'?s (?:ok|okay|fine|all right|alright)|all right|alright|sure|"
            r"yes|yeah|littlebot|little bot|robot|bot)")
_GA_WHAT = (r"(?:go ahead|go on|carry on|keep going|keep talking|continue|(?:please |you can |you )continue|"
            r"you go(?: ahead| on)?|you first|after you|as you were|(?:please )?finish(?: what you were saying| your "
            r"sentence| it| that)?|go for it|(?:what )?you were saying|what were you saying|sorry (?:to interrupt|for "
            r"interrupting|i interrupted)|don'?t mind me|ignore me|ignore that|not you|nothing|it'?s nothing|"
            r"(?:i )?(?:wasn'?t|was not) talking to you|(?:i was )?talking to (?:the baby|her|him|them|someone else)|"
            r"wrong (?:robot|one|person))")
# Their whole turn, after they cut in, says only "carry on" (a careful list; anything more is a real turn).
GO_AHEAD = re.compile(rf"^(?:{_GA_LEAD} )*{_GA_WHAT}(?: (?:{_GA_LEAD}|please|then|again|sorry))*$")


def is_go_ahead(text: str) -> bool:
    """"oops, go ahead", "sorry, carry on", "never mind, keep going" ...: they didn't mean to interrupt."""
    plain = " ".join(re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'")).split())
    return bool(plain) and GO_AHEAD.match(plain) is not None


def _latency_ms(stream: Any) -> float:
    """A stream's latency (PortAudio's figure, seconds) in ms for the log, 0 when it has none."""
    try:
        return max(0.0, float(getattr(stream, "latency", 0.0) or 0.0) * 1000)
    except (TypeError, ValueError):
        return 0.0


class LiveAudio:
    """Real devices through sounddevice (PortAudio). ``sd`` is injectable for tests."""

    stall_s = STALL_S

    def __init__(self, *, mic: int | str | None, speaker: int | str | None, volume: Callable[[], float],
                 mic_gain: Callable[[], float] = lambda: 1.0, half_duplex: bool = True, echo_tail_s: float = 0.45,
                 voice_level: float = 0.012, sd: Any = None, now: Callable[[], float] = time.monotonic,
                 open_retry_s: float = OPEN_RETRY_S, barge_in: BargeInSettings | None = None) -> None:
        self.mic_spec, self.speaker_spec = mic, speaker
        self._volume, self._mic_gain = volume, mic_gain
        self.half_duplex = half_duplex
        self.echo_tail_s = echo_tail_s
        self.voice_level = voice_level
        self._sd = sd
        self._now = now
        self._lock = threading.Lock()
        self._out: collections.deque[np.ndarray] = collections.deque()
        self._out_queued = 0
        self._in: collections.deque[np.ndarray] = collections.deque(maxlen=50)
        self._in_event: Any = None
        self._loop: Any = None
        self._in_stream: Any = None
        self._out_stream: Any = None
        self.mic_rate = RATE
        self.out_rate = RATE
        self._out_channels = 1
        self._in_resampler: Any = None
        self._pcm = np.zeros(0, dtype=np.int16)  # microphone audio at 48 kHz not yet sent
        self._out_resampler: Any = None
        self._muted = False
        self._vol, self._vol_at = 0.0, float("-inf")
        self._last_loud = float("-inf")
        self.level = 0.0  # last speaker block RMS (0..1)
        self._visemes: dsp.VisemeEstimator | None = None
        self._mouths: collections.deque[tuple[float, float, str]] = collections.deque(maxlen=64)  # (heard at, level, shape)
        self._mouth: tuple[float, str] = (0.0, "closed")
        self.mic_level = 0.0
        self.device_names = ("", "")
        # For the call's "still on" log line (littlebot/chatgpt/live.py, LiveCall._pulse): counts only, never sound.
        self.frames_in = self.frames_gated = self.frames_out = 0  # mic frames sent (of them silenced), voice frames
        self.peak_in = 0.0  # the loudest mic block since the last line
        self.open_retry_s = open_retry_s
        self._opened_at: float | None = None
        self.mic_at: float | None = None  # the latest block from the microphone (PortAudio's thread sets it)
        self.speaker_at: float | None = None  # the latest request for sound from the speaker
        # --- barge-in (see the module docstring); decided when the devices are open ---
        self.barge = barge_in or BargeInSettings(mode="off")
        self.echo_cancelled = False  # both devices are PipeWire's echo-cancelled ones
        self.barge_in_on = False
        self._detector: BargeInDetector | None = None
        self._bstate = _WATCH
        self._ring: collections.deque[np.ndarray] = collections.deque(maxlen=int(PREROLL_MAX_S / FRAME_S))
        self._sendq: collections.deque[np.ndarray] = collections.deque()  # the pre-roll, then the microphone
        self._send_pause = 0
        self._hold = False  # the voice fades out and waits in the queue (PortAudio's thread reads this)
        self._hold_gain = 1.0  # PortAudio's thread only
        self._queue_cap_s = MAX_QUEUED_S
        self._played: collections.deque[tuple[float, float]] = collections.deque(maxlen=48)  # (heard at, RMS)
        self._identify: Callable[..., Any] | None = None  # ctx.voices.identify (optional)
        self._on_barge: Callable[[str, dict[str, Any]], None] | None = None
        self._id_tasks: set[asyncio.Future[Any]] = set()
        self._barge_at = 0.0
        self._barge_run: _Run | None = None
        self._await_final = False  # their first final words after cutting in are still to come
        self._user_final = False
        self._user_loud_at = float("-inf")
        self._drop_at = 0.0
        self._frame_at: float | None = None  # the latest frame of the voice from the network
        self._quiet_since: float | None = None
        self.barge_ins = self.false_alarms = 0

    # ------------------------------------------------------------ lifecycle
    def open(self, loop: Any) -> None:
        sd = self._sd or dev.get_sounddevice()
        if sd is None:
            raise AudioDeviceError(f"PortAudio unavailable ({dev.sounddevice_error()})")
        self._loop = loop
        self._in_event = asyncio.Event()
        self._open_streams(sd)

    def reopen(self) -> None:
        """Close both streams and open them again (a stream stopped mid-call). Raises if they can't be opened.

        Blocking: call it from a worker thread. What the call reads keeps waiting on the same event, so
        the microphone simply carries on once it is back."""
        sd = self._sd or dev.get_sounddevice()
        if sd is None:
            raise AudioDeviceError(f"PortAudio unavailable ({dev.sounddevice_error()})")
        self._close_streams()
        try:
            self._open_streams(sd, retry_s=0.0)  # the same devices: quickest when the stream only stalled
            return
        except Exception as e:  # noqa: BLE001 - gone or reset: look for them again
            log.info("live audio: opening them again didn't work at once (%s); looking for them again", e)
        deadline = time.monotonic() + REOPEN_S
        while True:
            dev.refresh_portaudio()  # a USB device that was reset has a new index (done only while nothing is open)
            try:
                self._open_streams(sd)
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(REOPEN_STEP_S)  # a USB microphone takes a few seconds to come back after a reset

    def _open_streams(self, sd: Any, retry_s: float | None = None) -> None:
        deadline = time.monotonic() + max(0.0, self.open_retry_s if retry_s is None else retry_s)
        tries = 0
        while True:
            tries += 1
            try:
                mic, spk = self._start_streams(sd)
                break
            except Exception as e:  # busy for a moment (Device unavailable): try again shortly
                if time.monotonic() + OPEN_RETRY_STEP_S > deadline:
                    raise
                if tries == 1:
                    log.info("live audio: the speaker or microphone isn't free yet (%s); trying again", e)
                time.sleep(OPEN_RETRY_STEP_S)
        self.device_names = (mic["name"], spk["name"])
        self._opened_at = self._now()
        self.mic_at = self.speaker_at = None
        self._decide_barge_in(mic, spk)
        log.info("live audio: mic %s @ %d Hz, speaker %s @ %d Hz%s, latency mic %.0f ms / speaker %.0f ms; %s",
                 mic["name"], self.mic_rate, spk["name"], self.out_rate, f" (after {tries} tries)" if tries > 1 else "",
                 _latency_ms(self._in_stream), _latency_ms(self._out_stream), self._mode_note())

    def _decide_barge_in(self, mic: dict[str, Any], spk: dict[str, Any]) -> None:
        self.echo_cancelled = dev.is_echo_cancel_device(mic, "input") and dev.is_echo_cancel_device(spk, "output")
        mode = (self.barge.mode or "auto").lower()
        self.barge_in_on = self.half_duplex and (mode == "on" or (mode == "auto" and self.echo_cancelled))
        if self.barge_in_on and self._detector is None:
            self._detector = BargeInDetector(self.barge)  # kept across a reopen: the echo stays measured

    def _mode_note(self) -> str:
        ec = "echo cancelling on (PipeWire)" if self.echo_cancelled else "no echo cancelling"
        if self.barge_in_on:
            return f"{ec}: a grown-up can talk over the voice (barge-in on)"
        if not self.half_duplex:
            return f"{ec}, microphone always open (live_half_duplex: false)"
        why = "live_barge_in: off" if self.barge.mode == "off" else "needs echo cancelling"
        return f"{ec}: one at a time (half-duplex; barge-in off, {why})"

    def setup_barge_in(self, *, identify: Callable[..., Any] | None = None,
                       on_change: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        """The call's hooks: ``identify(samples, rate)`` (ctx.voices, optional) and ``on_change(what, info)``
        with what = "interrupted" (the voice stopped for them), "heard" (ChatGPT has their words) or
        "carried_on" (a false alarm: the voice plays on). Called in the event loop."""
        self._identify = identify if callable(identify) else None
        self._on_barge = on_change

    def _start_streams(self, sd: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        import av  # noqa: PLC0415  (comes with aiortc)

        mic = dev.find_device(self.mic_spec, "input")
        spk = dev.find_device(self.speaker_spec, "output")
        self.mic_rate = self._pick_rate(sd.check_input_settings, mic, 1)
        self.out_rate, self._out_channels = self._pick_output(sd, spk)
        self._in_resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        self._out_resampler = av.AudioResampler(format="s16", layout="mono", rate=self.out_rate)
        with dev._PA_LOCK:  # noqa: SLF001 - same lock the audio service uses around PortAudio
            self._out_stream = sd.OutputStream(device=spk["index"], samplerate=self.out_rate, channels=self._out_channels,
                                               dtype="float32", blocksize=self.out_rate // 50, latency="high",
                                               callback=self._play_cb)
            dev._stream_opened()  # noqa: SLF001
            try:
                self._in_stream = sd.InputStream(device=mic["index"], samplerate=self.mic_rate, channels=1,
                                                 dtype="int16", blocksize=self.mic_rate // 50, latency="low",
                                                 callback=self._record_cb)
                dev._stream_opened()  # noqa: SLF001
            except Exception:
                self._out_stream.close()
                dev._stream_closed()  # noqa: SLF001
                self._out_stream = None
                raise
        try:
            self._out_stream.start()
            self._in_stream.start()
        except Exception:
            self._close_streams()
            raise
        return mic, spk

    @staticmethod
    def _pick_rate(check: Callable[..., Any], d: dict[str, Any], channels: int) -> int:
        for rate in dev._rate_candidates(RATE, d.get("default_samplerate") or 0):  # noqa: SLF001
            try:
                check(device=d["index"], channels=channels, samplerate=rate, dtype="int16")
                return rate
            except Exception:  # noqa: BLE001
                continue
        raise AudioDeviceError(f"{d['name']}: no usable sample rate")

    def _pick_output(self, sd: Any, d: dict[str, Any]) -> tuple[int, int]:
        for channels in (1, 2):
            if channels > max(1, int(d.get("max_output_channels") or 1)):
                continue
            for rate in dev._rate_candidates(RATE, d.get("default_samplerate") or 0):  # noqa: SLF001
                try:
                    sd.check_output_settings(device=d["index"], channels=channels, samplerate=rate, dtype="float32")
                    return rate, channels
                except Exception:  # noqa: BLE001
                    continue
        raise AudioDeviceError(f"{d['name']}: no usable sample rate / channel layout")

    def close(self) -> None:
        self._close_streams()
        with self._lock:
            self._out.clear()
            self._out_queued = 0
            self._in.clear()
        self._pcm = np.zeros(0, dtype=np.int16)
        self.level = self.mic_level = 0.0
        self._mouths.clear()
        self._mouth = (0.0, "closed")
        self._opened_at = None
        for t in list(self._id_tasks):
            t.cancel()
        self._id_tasks.clear()
        self._ring.clear()
        self._sendq.clear()
        self._bstate, self._hold, self._barge_run, self._await_final = _WATCH, False, None, False

    def _close_streams(self) -> None:
        for name in ("_in_stream", "_out_stream"):
            stream = getattr(self, name)
            setattr(self, name, None)
            if stream is None:
                continue
            with dev._PA_LOCK:  # noqa: SLF001
                try:
                    stream.abort()  # not stop(): that waits for a stream that stalled (a USB device that went away)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    log.debug("closing live audio stream failed", exc_info=True)
                dev._stream_closed()  # noqa: SLF001

    # ------------------------------------------------------------ state
    def stalled(self) -> str | None:
        """"microphone" or "speaker" when that stream has given nothing for STALL_S (it stopped), else None."""
        if self._opened_at is None:
            return None
        now = self._now()
        if now - (self.mic_at if self.mic_at is not None else self._opened_at) >= self.stall_s:
            return "microphone"
        if now - (self.speaker_at if self.speaker_at is not None else self._opened_at) >= self.stall_s:
            return "speaker"
        return None

    @property
    def speaking(self) -> bool:
        """The model's voice is audible now (or was within the echo tail); never while someone cut in."""
        return self._bstate == _WATCH and self._now() - self._last_loud < self.echo_tail_s

    @property
    def mic_open(self) -> bool:
        return self._bstate != _WATCH or not (self.half_duplex and self.speaking)

    @property
    def interrupted(self) -> bool:
        """Someone cut in: the voice is held (deciding) or the turn they cut into is being dropped."""
        return self._bstate != _WATCH

    @property
    def mouth(self) -> tuple[float, str]:
        """``(level 0..1, viseme)`` of the voice heard right now, for the face's lip-sync.

        Blocks are stamped with when the speaker will play them, so this follows the sound, not
        the network; it runs :data:`MOUTH_LEAD_S` early to make up for the face's own delay.
        """
        now = self._now() + MOUTH_LEAD_S
        q = self._mouths
        while q and q[0][0] <= now:  # only this reader pops; the audio thread only appends
            _, level, shape = q.popleft()
            self._mouth = (level, shape)
        return self._mouth

    def mute(self) -> None:
        """Silence the speaker at once and drop whatever is queued (safety cut, hang-up)."""
        self._muted = True
        with self._lock:
            self._out.clear()
            self._out_queued = 0
        self._mouths.clear()
        self._mouth = (0.0, "closed")

    # ------------------------------------------------------------ microphone
    def _record_cb(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:  # PortAudio thread
        block = np.array(indata[:, 0], dtype=np.int16, copy=True)
        self.mic_at = self._now()
        with self._lock:
            self._in.append(block)
        loop, ev = self._loop, self._in_event
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # loop closed
                pass

    async def _next_block(self) -> np.ndarray:
        while True:
            with self._lock:
                block = self._in.popleft() if self._in else None
                if not self._in and self._in_event is not None:
                    self._in_event.clear()
            if block is not None:
                return block
            await self._in_event.wait()

    async def read_frame(self) -> np.ndarray:
        """The next 20 ms of microphone audio at 48 kHz (int16, mono); silence while the gate is shut."""
        import av  # noqa: PLC0415

        while len(self._pcm) < FRAME:
            block = await self._next_block()
            gain = max(0.0, min(4.0, float(self._mic_gain())))
            if gain != 1.0:
                block = np.clip(block.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
            self.mic_level = float(np.sqrt(np.mean((block.astype(np.float32) / 32768.0) ** 2))) if len(block) else 0.0
            if self.mic_rate != RATE:
                frame = av.AudioFrame.from_ndarray(block.reshape(1, -1), format="s16", layout="mono")
                frame.sample_rate = self.mic_rate
                parts = [f.to_ndarray().reshape(-1) for f in self._in_resampler.resample(frame)]
                block = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
            self._pcm = np.concatenate([self._pcm, block.astype(np.int16)])
        out, self._pcm = self._pcm[:FRAME], self._pcm[FRAME:]
        if len(self._pcm) > RATE // 5:  # never let the microphone lag behind
            self._pcm = self._pcm[-FRAME:]
        self.frames_in += 1
        self.peak_in = max(self.peak_in, self.mic_level)
        if self.barge_in_on and self._detector is not None:
            return self._barge_frame(out)
        if not self.mic_open:
            self.frames_gated += 1
            return np.zeros(FRAME, dtype=np.int16)
        return out

    # ------------------------------------------------------------ barge-in
    def _echo_ref(self, now: float) -> float:
        """The loudest the speaker played in the last ECHO_REF_S: what could still echo at the microphone now."""
        played = list(self._played)  # appended by PortAudio's thread; copying a deque holds the GIL
        return max((lvl for at, lvl in played if now - ECHO_REF_S <= at <= now + FRAME_S), default=0.0)

    def _barge_frame(self, out: np.ndarray) -> np.ndarray:
        """The half-duplex gate with barge-in: what to send for this 20 ms of microphone (event loop)."""
        now = self._now()
        det = self._detector
        assert det is not None
        self._ring.append(out)
        if self._bstate == _WATCH:
            if now - self._last_loud < self.echo_tail_s:  # the voice is audible: watch for someone cutting in
                if det.step(out, self._echo_ref(now), now, voice_on=True):
                    self._interrupt(now)
                    return self._send(None)
                self._maybe_identify(now)
                self.frames_gated += 1
                self._sendq.clear()
                return np.zeros(FRAME, dtype=np.int16)
            det.step(out, 0.0, now, voice_on=False)  # the room's own noise
            return self._send(out)
        det.step(out, 0.0, now, voice_on=False)  # they cut in: are they still talking?
        if det.level >= det.threshold:
            self._user_loud_at = now
        if self._bstate == _HELD:
            self._check_held(now)
            if self._bstate == _WATCH:  # a false alarm: the voice plays on, the gate is shut again
                self.frames_gated += 1
                return np.zeros(FRAME, dtype=np.int16)
        return self._send(out)

    def _send(self, frame: np.ndarray | None) -> np.ndarray:
        """The pre-roll first, then the microphone, one frame per frame read (never a burst). While behind,
        pauses longer than KEEP_PAUSE_S are cut short, so it is back in real time at their first pause."""
        if frame is not None:
            if not self._sendq:
                return frame
            self._sendq.append(frame)
        if not self._sendq:
            return np.zeros(FRAME, dtype=np.int16)
        f = self._sendq.popleft()
        quiet = float(np.sqrt(np.mean((f.astype(np.float32) / 32768.0) ** 2))) < QUIET_LEVEL
        self._send_pause = self._send_pause + 1 if quiet else 0
        if quiet and self._sendq and self._send_pause > KEEP_PAUSE_S / FRAME_S:
            f = self._sendq.popleft()  # one frame of the pause left out
        return f

    def _maybe_identify(self, now: float) -> None:
        """Ask voice recognition (ctx.voices) once per run, in the background: a grown-up interrupts sooner,
        a child not at all. The detector never waits for it."""
        run = self._detector.run if self._detector is not None else None
        if run is None or run.asked or self._identify is None or now - run.onset < ID_AFTER_S:
            return
        run.asked = True
        frames = max(1, round((now - run.onset) / FRAME_S))
        pcm = np.concatenate(list(self._ring)[-frames:]).astype(np.float32) / 32768.0
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # not in the event loop (a test driving the gate by hand)
            return
        task = loop.create_task(self._identify_run(run, dsp.resample(pcm, RATE, 16000)))
        self._id_tasks.add(task)
        task.add_done_callback(self._id_tasks.discard)

    async def _identify_run(self, run: _Run, samples: np.ndarray) -> None:
        try:
            match = await asyncio.wait_for(self._identify(samples, 16000), ID_TIMEOUT_S)  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - voice recognition is optional: the detector decides alone
            log.debug("voice recognition for barge-in failed", exc_info=True)
            return
        finally:
            del samples
        if match is None:
            return
        run.voice = str(getattr(match, "name", "") or "") or None
        if getattr(match, "is_child", False):
            run.vetoed = True
            if self._bstate == _HELD and self._barge_run is run:
                self._carry_on("voice recognition says it was a child")
        else:
            run.grown_up = True

    def _interrupt(self, now: float) -> None:
        """A grown-up is talking over the voice: fade it out and hold it, send them from their first word."""
        det = self._detector
        run = det.fired if det is not None else None
        onset = run.onset if run is not None else now - self.barge.speech_s
        self._bstate, self._barge_at, self._barge_run = _HELD, now, run
        self._await_final = True
        self._user_final = False
        self._user_loud_at = now
        self._queue_cap_s = max(MAX_QUEUED_S, HOLD_MAX_S)
        self._hold = True
        self._mouths.clear()
        self._mouth = (0.0, "closed")
        frames = min(len(self._ring), math.ceil((now - onset + PREROLL_LEAD_S) / FRAME_S))
        self._sendq = collections.deque(list(self._ring)[-frames:])
        self._send_pause = 0
        self.barge_ins += 1
        pitch = BargeInDetector.pitch_of(run) if run is not None else 0.0
        voice = run.voice if run is not None else None
        log.info("live voice: interrupted by a grown-up after %.1f s of speech (pitch %d Hz%s)", now - onset,
                 round(pitch), f", voice {voice}" if voice else "")
        self._notify("interrupted", {"speech_s": round(now - onset, 2), "pitch_hz": round(pitch), "voice": voice})

    def _check_held(self, now: float) -> None:
        """Held, and no words from ChatGPT: a false alarm once they stop (at the latest FALSE_ALARM_MAX_S)."""
        held = now - self._barge_at
        still = now - self._user_loud_at < STILL_TALKING_S
        if held >= self.barge.false_alarm_s and (not still or held >= max(FALSE_ALARM_MAX_S, self.barge.false_alarm_s)):
            self._carry_on(f"no words within {held:.1f} s")

    def _carry_on(self, reason: str) -> None:
        """A false alarm: let the held voice play on and shut the gate again."""
        if self._bstate != _HELD:
            return
        self._bstate, self._barge_run, self._await_final = _WATCH, None, False
        self._hold = False
        if self._out_queued > 0:
            self._last_loud = self._now()  # the voice comes back now: no gap in the gate
        self._sendq.clear()
        if self._detector is not None:
            self._detector.reset_run()
        self.false_alarms += 1
        log.info("live voice: false alarm (%s), the voice carried on", reason)
        self._notify("carried_on", {"reason": reason})

    def barge_in_heard(self, text: str, final: bool) -> str | None:
        """ChatGPT's caption of what they say (littlebot/chatgpt/live.py passes every one). Their words while the
        voice is held: it was them, the held voice is dropped. Returns "go ahead" when their first final words
        after cutting in say only that ("oops, go ahead"): the voice should carry on where it stopped."""
        if not self.barge_in_on or not text.strip():
            return None
        if self._bstate == _HELD:
            self._confirm()
        if final and self._await_final:
            self._await_final = False
            self._user_final = True
            if is_go_ahead(text):
                return "go ahead"
        return None

    def _confirm(self) -> None:
        now = self._now()
        self._bstate, self._drop_at = _DROPPING, now
        with self._lock:
            self._out.clear()
            self._out_queued = 0
        self._hold = False
        self._queue_cap_s = MAX_QUEUED_S
        self._quiet_since = None
        log.info("live voice: ChatGPT heard them %d ms after they cut in; the rest of that turn is dropped",
                  int((now - self._barge_at) * 1000))
        self._notify("heard", {})

    def drop_voice(self) -> None:
        """They asked the voice to be quiet: what it was about to say is dropped at once (a short fade of the
        block playing is all that is left) and the microphone is theirs again."""
        with self._lock:
            self._out.clear()
            self._out_queued = 0
        self._hold = False
        self._queue_cap_s = MAX_QUEUED_S
        if self._bstate != _WATCH:
            self._bstate, self._barge_run, self._await_final = _WATCH, None, False
            self._sendq.clear()
            if self._detector is not None:
                self._detector.reset_run()

    def _dropping(self, pcm: np.ndarray) -> bool:
        """While the turn they cut into may still arrive: True = drop this. The next turn is sound after a pause
        of DROP_GAP_S, once they have stopped talking (or their words are final); at the latest DROP_MAX_S."""
        now = self._now()
        last = self._frame_at
        if self._quiet_since is None and last is not None and now - last >= DROP_GAP_S:
            self._quiet_since = last  # nothing arrived for a while: that was a pause too
        loud = len(pcm) > 0 and float(np.sqrt(np.mean(pcm * pcm))) >= self.voice_level
        if not loud:
            self._quiet_since = self._quiet_since if self._quiet_since is not None else now
            return True
        paused = self._quiet_since is not None and now - self._quiet_since >= DROP_GAP_S
        done = self._user_final or now - self._user_loud_at >= USER_DONE_S
        if (paused and done) or now - self._drop_at >= DROP_MAX_S:
            self._bstate = _WATCH
            log.debug("live voice: the voice's next turn starts %.1f s after they cut in", now - self._barge_at)
            return False
        self._quiet_since = None
        return True

    def _notify(self, what: str, info: dict[str, Any]) -> None:
        cb = self._on_barge
        if cb is None:
            return
        try:
            cb(what, info)
        except Exception:  # noqa: BLE001 - the call's bookkeeping must never break the microphone
            log.debug("barge-in callback failed", exc_info=True)

    # ------------------------------------------------------------ speaker
    def play_frame(self, frame: Any) -> None:
        """Queue one decoded frame of the model's voice (an av.AudioFrame from the remote track)."""
        if self._muted:
            return
        self.frames_out += 1
        for f in self._out_resampler.resample(frame):
            pcm = f.to_ndarray().reshape(-1).astype(np.float32) / 32768.0
            if self._bstate == _DROPPING and self._dropping(pcm):
                continue
            with self._lock:
                self._out.append(pcm)
                self._out_queued += len(pcm)
                if not self._hold and self._out_queued <= MAX_QUEUED_S * self.out_rate:
                    self._queue_cap_s = MAX_QUEUED_S  # a held voice that played on has caught up again
                while self._out_queued > self._queue_cap_s * self.out_rate and len(self._out) > 1:
                    self._out_queued -= len(self._out.popleft())
        self._frame_at = self._now()

    def _take(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < n and self._out:
                head = self._out[0]
                k = min(n - filled, len(head))
                out[filled:filled + k] = head[:k]
                filled += k
                if k == len(head):
                    self._out.popleft()
                else:
                    self._out[0] = head[k:]
                self._out_queued -= k
        return out

    def _output_delay(self, time_info: Any) -> float:
        """Seconds until the block being filled is heard: PortAudio's DAC time, else the stream latency."""
        try:
            delay = float(time_info.outputBufferDacTime) - float(time_info.currentTime)
            if 0.0 < delay <= MAX_OUTPUT_DELAY_S:
                return delay
        except Exception:  # noqa: BLE001 - no timing info (some hosts / fakes): use the latency
            pass
        try:
            latency = float(getattr(self._out_stream, "latency", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return latency if 0.0 <= latency <= MAX_OUTPUT_DELAY_S else 0.0

    def _faded(self, frames: int) -> np.ndarray:
        """The next block while the voice fades out to be held (it then waits in the queue) or back in."""
        if self._hold and self._hold_gain <= 0.0:
            return np.zeros(frames, dtype=np.float32)
        y = self._take(frames)
        step = 1.0 / max(1.0, FADE_S * self.out_rate)
        ramp = self._hold_gain + (-step if self._hold else step) * np.arange(1, frames + 1, dtype=np.float32)
        ramp = np.clip(ramp, 0.0, 1.0)
        self._hold_gain = float(ramp[-1]) if frames else self._hold_gain
        return y * ramp

    def _play_cb(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:  # PortAudio thread
        if self._muted:
            y = np.zeros(frames, dtype=np.float32)
        elif self._hold or self._hold_gain < 1.0:
            y = self._faded(frames)
        else:
            y = self._take(frames)
        rms = float(np.sqrt(np.mean(y * y))) if frames else 0.0
        self.level = rms
        now = self.speaker_at = self._now()
        if rms >= self.voice_level:
            self._last_loud = now
        try:  # lip-sync for the face: must never break the voice (an exception here stops the stream)
            est = self._visemes
            if est is None or est.sample_rate != self.out_rate:
                est = self._visemes = dsp.VisemeEstimator(self.out_rate)
            level, shape = est.process(y) if frames else (0.0, "closed")
            self._mouths.append((now + self._output_delay(time_info), level, shape))
        except Exception:  # noqa: BLE001
            log.debug("lip-sync estimate failed", exc_info=True)
        if now - self._vol_at > 0.25:  # re-read the volume a few times a second, not every block
            self._vol_at, self._vol = now, max(0.0, min(1.0, float(self._volume())))
        if self.barge_in_on:  # what the speaker plays, for the barge-in detector's echo measure
            self._played.append((now + self._output_delay(time_info), rms * self._vol))
        y = y * self._vol
        outdata[:] = np.repeat(y[:, None], self._out_channels, axis=1) if self._out_channels > 1 else y[:, None]


class CarryFeed:
    """The microphone of a call the wake word started: what was said with the wake word first, then ``audio``.

    ``start()`` (once the devices are open) reads ``audio.read_frame()`` all the time from then on, so what is
    said while the call connects waits here too (LiveAudio itself keeps only the last second) and the
    half-duplex gate is applied as it is heard, not when it is sent. ``read_frame()`` (the call's MicTrack)
    gives one 20 ms frame per 20 ms, never faster than real time: a burst could be dropped or mistimed by the
    far end. While frames are waiting, each pause is cut to KEEP_PAUSE_S, so the call is back in real time
    at their first pause. Timestamps stay continuous: MicTrack numbers the frames it is given.

    ``carry``: a littlebot.audio.carry.SpeechCarry (taken once; its audio is never kept anywhere else).
    """

    def __init__(self, audio: Any, carry: Any) -> None:
        self.audio = audio
        self._carry = carry
        self.quiet_level = float(getattr(carry, "quiet_level", 0.0105))
        self._waiting: collections.deque[np.ndarray] = collections.deque()
        self._arrived = asyncio.Event()
        self._puller: Any = None
        self._due: float | None = None
        self._pause = 0  # quiet frames in a row
        self._keep_pause = int(round(KEEP_PAUSE_S * RATE / FRAME))
        self._carried = 0  # frames of the carried speech still waiting
        self._on_at: float | None = None  # the first frame asked for: the call's sound flows
        self._first_heard: float | None = None  # the first frame from the call's own microphone
        self.sent = self.skipped = self.lost = 0
        self.caught_up = False
        samples = carry.take() if carry is not None else np.zeros(0, dtype=np.float32)
        rate = int(getattr(carry, "sample_rate", RATE) or RATE)
        if len(samples):
            pcm = dsp.to_int16(dsp.resample(dsp.fade(samples, rate, 0.01, 0.01), rate, RATE))  # no click at the cut
            pcm = np.concatenate([pcm, np.zeros(-len(pcm) % FRAME, dtype=np.int16)])
            self._waiting.extend(row.copy() for row in pcm.reshape(-1, FRAME))
            del samples, pcm
        self._carried = len(self._waiting)
        self.carried_s = self._carried * FRAME / RATE
        self.carried_speech_s = sum(1 for f in self._waiting if self._loud(f)) * FRAME / RATE

    def start(self, spawn: Callable[[Any], Any] = asyncio.ensure_future) -> None:
        """Start reading the microphone (``spawn`` makes the task; the call's own, so it ends with the call)."""
        if self._puller is None:
            self._puller = spawn(self._pull())

    def close(self) -> None:
        if self._puller is not None and not self._puller.done():
            self._puller.cancel()
        self._waiting.clear()
        self._arrived.set()
        self._carry = None

    @property
    def waiting_s(self) -> float:
        return len(self._waiting) * FRAME / RATE

    async def _pull(self) -> None:
        try:
            while True:
                frame = await self.audio.read_frame()
                if self._first_heard is None:
                    self._first_heard = time.monotonic()
                self._waiting.append(frame)
                while len(self._waiting) > MAX_WAITING_S * RATE / FRAME:  # far behind (a very slow connect)
                    self._waiting.popleft()
                    self._carried = max(0, self._carried - 1)
                    self.lost += 1
                self._arrived.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live voice: reading the microphone failed")
            raise
        finally:
            self._arrived.set()  # a waiting reader then reads the microphone itself

    async def read_frame(self) -> np.ndarray:
        """The next 20 ms to send (int16, 48 kHz)."""
        if self._on_at is None:
            self._on_at = time.monotonic()
            self._log_start()
        await self._beat()
        while True:
            frame, waited = await self._next()
            if waited:  # nothing was waiting: the microphone sets the pace from here
                self._due = time.monotonic() + FRAME / RATE
            quiet = not self._loud(frame)
            self._pause = self._pause + 1 if quiet else 0
            if quiet and self._pause > self._keep_pause and len(self._waiting) >= BEHIND_FRAMES:
                self.skipped += 1  # a long pause while behind: catch up
                continue
            self.sent += 1
            if not self.caught_up and not self._carried and len(self._waiting) < BEHIND_FRAMES:
                self.caught_up = True
                log.info("live voice: caught up with real time %d ms after the call's microphone came on "
                         "(%.1f s of pauses left out)", int((time.monotonic() - self._on_at) * 1000),
                         self.skipped * FRAME / RATE)
            return frame

    async def _beat(self) -> None:
        """Wait for this frame's 20 ms slot (a slot missed by more than PACE_SLACK_S restarts the beat)."""
        now = time.monotonic()
        if self._due is None or now - self._due > PACE_SLACK_S:
            self._due = now
        elif self._due > now:
            await asyncio.sleep(self._due - now)
        self._due += FRAME / RATE

    async def _next(self) -> tuple[np.ndarray, bool]:
        waited = False
        while not self._waiting:
            if self._puller is None or self._puller.done():
                return await self.audio.read_frame(), True
            waited = True
            self._arrived.clear()
            await self._arrived.wait()
        if self._carried:
            self._carried -= 1
        return self._waiting.popleft(), waited

    def _loud(self, frame: np.ndarray) -> bool:
        x = frame.astype(np.float32)
        return float(np.sqrt(np.mean(x * x))) / 32768.0 >= self.quiet_level if len(x) else False

    def _log_start(self) -> None:
        carry = self._carry
        now = time.monotonic()
        heard_at = getattr(carry, "heard_at", None)
        handed_at = getattr(carry, "sealed_at", None)
        gap = ""
        if handed_at is not None and self._first_heard is not None:
            gap = f"; microphone hand-over gap about {max(0, int((self._first_heard - handed_at) * 1000) - 20)} ms"
        log.info("live voice: microphone on%s; sending first the %.1f s carried over from the wake word (%.1f s of "
                 "speech) and the %.1f s heard while connecting%s",
                 f" {int((now - heard_at) * 1000)} ms after the wake phrase" if heard_at is not None else "",
                 self.carried_s, self.carried_speech_s, max(0, len(self._waiting) - self._carried) * FRAME / RATE, gap)
