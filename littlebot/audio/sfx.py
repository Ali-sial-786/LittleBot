"""Procedural, child-friendly sound effects and a tiny music-box melody synth.

Design rules for every sound (LittleBot plays these next to a ~10-month-old):

* soft timbres: sines with a few weak harmonics, music-box / bell / xylophone
  colours, band-limited noise only where needed (clap, whoosh, drum);
* no startling transients: every onset has a >= 6 ms raised-cosine attack,
  tails decay exponentially, and a gentle low-pass removes edge harshness;
* moderate loudness: peaks normalised to about -6 dBFS and RMS capped
  (``MAX_RMS``), so the master volume and its safety ceiling stay meaningful;
* deterministic: fixed random seeds, so ``scripts/generate_sounds.py`` writes
  identical WAVs every time (``assets/sounds/<name>.wav``, mono 16-bit 22.05 kHz).

``SoundBank`` loads those WAVs at runtime and synthesises any missing one.
"""

from __future__ import annotations

import logging
import math
import random
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from littlebot.audio import dsp
from littlebot.core.interfaces import SOUND_NAMES
from littlebot.core.paths import SOUNDS_DIR

log = logging.getLogger("littlebot.audio.sfx")

SAMPLE_RATE = 22050
TARGET_PEAK = 0.5  # about -6 dBFS
MAX_RMS = 0.16  # about -16 dBFS: moderate loudness even for sustained sounds
MIN_ATTACK = 0.006  # seconds; nothing starts faster than this

TWO_PI = 2.0 * math.pi


# =========================================================== synthesis primitives
def _n(dur: float, sr: int) -> int:
    return max(1, int(round(dur * sr)))


def _phase(freq: float | np.ndarray, n: int, sr: int) -> np.ndarray:
    f = np.broadcast_to(np.asarray(freq, dtype=np.float64), (n,))
    return TWO_PI * np.cumsum(f) / sr


def _attack(n: int, sr: int, attack: float) -> np.ndarray:
    attack = max(MIN_ATTACK, attack)
    t = np.arange(n) / sr
    a = np.clip(t / attack, 0.0, 1.0)
    return 0.5 - 0.5 * np.cos(math.pi * a)


def _perc(n: int, sr: int, attack: float = 0.008, tau: float = 0.3) -> np.ndarray:
    """Smooth attack then exponential decay (plucked / struck sounds)."""
    t = np.arange(n) / sr
    return _attack(n, sr, attack) * np.exp(-np.maximum(t - attack, 0.0) / max(1e-3, tau))


def _adsr(n: int, sr: int, attack: float = 0.01, decay: float = 0.08, sustain: float = 0.6,
          release: float = 0.08) -> np.ndarray:
    attack = max(MIN_ATTACK, attack)
    t = np.arange(n) / sr
    total = n / sr
    env = np.where(t < attack, 0.5 - 0.5 * np.cos(math.pi * np.clip(t / attack, 0, 1)),
                   sustain + (1.0 - sustain) * np.exp(-(t - attack) / max(1e-3, decay)))
    rel_start = max(attack, total - release)
    rel = np.clip((t - rel_start) / max(1e-3, release), 0.0, 1.0)
    return env * (0.5 + 0.5 * np.cos(math.pi * rel))


def _tone(freq: float | np.ndarray, n: int, sr: int,
          harmonics: Sequence[tuple[float, float]] = ((1.0, 1.0),)) -> np.ndarray:
    ph = _phase(freq, n, sr)
    out = np.zeros(n)
    for mult, amp in harmonics:
        out += amp * np.sin(ph * mult)
    return out


def _pluck(freq: float, dur: float, sr: int, tau: float = 0.5, bright: float = 0.3) -> np.ndarray:
    """Music-box / soft xylophone note: fundamental + quickly fading overtones."""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    a = _attack(n, sr, 0.006)
    x = np.sin(TWO_PI * freq * t) * np.exp(-t / tau)
    x += bright * 0.5 * np.sin(TWO_PI * 2 * freq * t) * np.exp(-t / (tau * 0.45))
    x += bright * 0.22 * np.sin(TWO_PI * 3 * freq * t) * np.exp(-t / (tau * 0.25))
    x += bright * 0.10 * np.sin(TWO_PI * 4.02 * freq * t) * np.exp(-t / (tau * 0.15))
    return x * a


def _bell(freq: float, dur: float, sr: int, tau: float = 0.9) -> np.ndarray:
    """Soft bell: slightly inharmonic partials, the high ones fading fastest."""
    n = _n(dur, sr)
    t = np.arange(n) / sr
    partials = ((1.0, 1.0, 1.0), (2.0, 0.32, 0.6), (2.76, 0.16, 0.4), (4.07, 0.06, 0.25), (5.4, 0.03, 0.18))
    x = np.zeros(n)
    for mult, amp, tmul in partials:
        if freq * mult > 7000:
            continue
        x += amp * np.sin(TWO_PI * freq * mult * t) * np.exp(-t / (tau * tmul))
    return x * _attack(n, sr, 0.007)


def _noise(n: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).standard_normal(n)


def _bandpass_noise(n: int, seed: int, lo_width: int, hi_width: int) -> np.ndarray:
    """Crude band-pass: short moving average (low-pass) minus a long one (removes lows)."""
    x = _noise(n, seed)
    return dsp.moving_average(x, lo_width) - dsp.moving_average(x, hi_width)


def _mix(total: float, sr: int, parts: Iterable[tuple[float, np.ndarray]]) -> np.ndarray:
    out = np.zeros(_n(total, sr))
    for start, part in parts:
        i = int(round(start * sr))
        if i >= len(out):
            continue
        k = min(len(part), len(out) - i)
        out[i:i + k] += part[:k]
    return out


def _master(x: np.ndarray, sr: int, *, peak: float = TARGET_PEAK, lowpass_passes: int = 1) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    x = x - float(np.mean(x))
    y = dsp.soft_lowpass(x.astype(np.float32), passes=lowpass_passes)
    y = dsp.fade(y, sr, fade_in=0.004, fade_out=0.03)
    y = dsp.normalize_peak(y, peak)
    r = dsp.rms(y)
    if r > MAX_RMS:
        y = (y * (MAX_RMS / r)).astype(np.float32)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def _hz(note: str) -> float:
    f = parse_note(note)
    assert f is not None
    return f


# ================================================================== the sounds
def _chime(sr: int) -> np.ndarray:
    return _mix(1.35, sr, [(0.0, _bell(_hz("G5"), 1.2, sr, 0.55)), (0.14, _bell(_hz("C6"), 1.2, sr, 0.65))])


def _pop(sr: int) -> np.ndarray:
    n = _n(0.18, sr)
    t = np.arange(n) / sr
    f = 360 + 380 * np.exp(-t / 0.03)
    return _tone(f, n, sr, ((1, 1.0), (2, 0.12))) * _perc(n, sr, 0.006, 0.05)


def _boop(sr: int) -> np.ndarray:
    n = _n(0.26, sr)
    t = np.arange(n) / sr
    f = 390 + 130 * np.exp(-t / 0.06)
    return _tone(f, n, sr, ((1, 1.0), (2, 0.2), (3, 0.05))) * _adsr(n, sr, 0.012, 0.06, 0.7, 0.09)


def _boing(sr: int) -> np.ndarray:
    n = _n(0.65, sr)
    t = np.arange(n) / sr
    f = 240 * (1 + 0.28 * np.exp(-t / 0.22) * np.sin(TWO_PI * 11 * t)) * (1 + 0.25 * t)
    return _tone(f, n, sr, ((1, 1.0), (2, 0.25), (3, 0.06))) * _perc(n, sr, 0.008, 0.22)


def _peekaboo(sr: int) -> np.ndarray:
    n = _n(0.5, sr)
    t = np.arange(n) / sr
    glide = _hz("A5") + (_hz("C6") - _hz("A5")) * np.clip(t / 0.08, 0, 1)
    glide = glide * (1 + 0.012 * np.sin(TWO_PI * 5.5 * t))
    boo = _tone(glide, n, sr, ((1, 1.0), (2, 0.18), (3, 0.05))) * _adsr(n, sr, 0.015, 0.12, 0.55, 0.18)
    return _mix(0.95, sr, [(0.0, _pluck(_hz("E5"), 0.3, sr, 0.18)), (0.15, _pluck(_hz("G5"), 0.3, sr, 0.18)),
                           (0.36, boo * 0.9)])


def _tada(sr: int) -> np.ndarray:
    grace = _pluck(_hz("G5"), 0.2, sr, 0.1, 0.2)
    n = _n(1.0, sr)
    t = np.arange(n) / sr
    vib = 1 + 0.004 * np.sin(TWO_PI * 5 * t)
    chord = np.zeros(n)
    for note, amp in (("C5", 0.8), ("E5", 0.6), ("G5", 0.55), ("C6", 0.35)):
        chord += amp * _tone(_hz(note) * vib, n, sr, ((1, 1.0), (2, 0.3), (3, 0.12)))
    chord *= _adsr(n, sr, 0.02, 0.25, 0.45, 0.35)
    return _mix(1.15, sr, [(0.0, grace * 0.8), (0.11, chord)])


def _clap(sr: int) -> np.ndarray:
    parts = []
    for i, start in enumerate((0.0, 0.27, 0.54)):
        n = _n(0.16, sr)
        body = _bandpass_noise(n, 100 + i, 4, 30) * _perc(n, sr, 0.006, 0.03)
        thump = np.sin(TWO_PI * 210 * np.arange(n) / sr) * _perc(n, sr, 0.006, 0.025) * 0.25
        parts.append((start, (body * 2.0 + thump) * (1.0 - 0.1 * i)))
    return _mix(0.8, sr, parts)


def _whoosh(sr: int) -> np.ndarray:
    n = _n(0.75, sr)
    t = np.arange(n) / sr
    x = _noise(n, 7)
    dark = dsp.moving_average(x, 14)
    bright = dsp.moving_average(x, 4) - dsp.moving_average(x, 40)
    g = np.exp(-0.5 * ((t - 0.36) / 0.13) ** 2)
    b = np.exp(-0.5 * ((t - 0.38) / 0.09) ** 2)
    return (dark * (1 - 0.6 * b) + 0.35 * bright * b) * g


def _sparkle(sr: int) -> np.ndarray:
    seq = ("C6", "E6", "G6", "A6", "G6", "C7", "E6")
    parts = []
    for i, note in enumerate(seq):
        parts.append((i * 0.085, _bell(_hz(note), 0.45, sr, 0.22) * (1.0 - 0.07 * i)))
    return _mix(1.0, sr, parts)


def _drum(sr: int) -> np.ndarray:
    def hit(seed: int) -> np.ndarray:
        n = _n(0.4, sr)
        t = np.arange(n) / sr
        f = 95 + 60 * np.exp(-t / 0.04)
        tone = _tone(f, n, sr, ((1, 1.0), (2, 0.45), (3, 0.12))) * _perc(n, sr, 0.007, 0.15)
        skin = dsp.moving_average(_noise(n, seed), 18) * _perc(n, sr, 0.006, 0.02) * 1.5
        return tone + skin
    return _mix(0.7, sr, [(0.0, hit(11)), (0.22, hit(12) * 0.8)])


def _bell_sound(sr: int) -> np.ndarray:
    return _bell(_hz("A5"), 1.6, sr, 0.85)


def _giggle(sr: int) -> np.ndarray:
    parts = []
    for i, start in enumerate((0.0, 0.12, 0.24, 0.36, 0.5)):
        n = _n(0.095, sr)
        t = np.arange(n) / sr
        f0 = 720 - 28 * i
        f = f0 * (1 + 0.09 * np.sin(math.pi * t / (n / sr)))
        syl = _tone(f, n, sr, ((1, 1.0), (2, 0.35), (3, 0.12))) * _adsr(n, sr, 0.008, 0.03, 0.7, 0.035)
        parts.append((start, syl * (1.0 - 0.08 * i)))
    return _mix(0.65, sr, parts)


def _uhoh(sr: int) -> np.ndarray:
    voice = ((1, 1.0), (2, 0.5), (3, 0.22), (4, 0.08))
    n1 = _n(0.24, sr)
    t1 = np.arange(n1) / sr
    uh = _tone(_hz("G4") - 25 * t1 / t1[-1], n1, sr, voice) * _adsr(n1, sr, 0.015, 0.06, 0.75, 0.06)
    n2 = _n(0.42, sr)
    t2 = np.arange(n2) / sr
    oh = _tone(_hz("E4") - 45 * t2 / t2[-1], n2, sr, voice) * _adsr(n2, sr, 0.02, 0.1, 0.65, 0.15)
    return _mix(0.78, sr, [(0.0, uh), (0.3, oh)])


def _yay(sr: int) -> np.ndarray:
    n = _n(0.6, sr)
    t = np.arange(n) / sr
    rise = np.clip(t / 0.32, 0, 1)
    f = 440 + 440 * (1 - (1 - rise) ** 2)
    f = f * (1 + 0.015 * np.sin(TWO_PI * 6 * t) * rise)
    voice = _tone(f, n, sr, ((1, 1.0), (2, 0.3), (3, 0.1))) * _adsr(n, sr, 0.02, 0.15, 0.7, 0.15)
    return _mix(0.85, sr, [(0.0, voice), (0.42, _bell(_hz("E6"), 0.4, sr, 0.15) * 0.3),
                           (0.5, _bell(_hz("G6"), 0.35, sr, 0.15) * 0.3)])


def _blip(sr: int) -> np.ndarray:
    n = _n(0.1, sr)
    return _tone(_hz("A5"), n, sr, ((1, 1.0), (2, 0.1))) * _adsr(n, sr, 0.006, 0.03, 0.6, 0.05)


def _notes(sr: int) -> np.ndarray:
    seq = (("C5", 0.0), ("E5", 0.16), ("G5", 0.32), ("E5", 0.48), ("C6", 0.64))
    return _mix(1.2, sr, [(start, _pluck(_hz(note), 0.6, sr, 0.35)) for note, start in seq])


def _sleep(sr: int) -> np.ndarray:
    n = _n(1.6, sr)
    pad = _tone(_hz("C4"), n, sr, ((1, 1.0), (2, 0.15))) * _adsr(n, sr, 0.35, 0.5, 0.8, 0.6) * 0.35
    return _mix(1.7, sr, [(0.0, pad), (0.05, _pluck(_hz("G5"), 0.7, sr, 0.45, 0.15)),
                          (0.4, _pluck(_hz("E5"), 0.7, sr, 0.45, 0.15)),
                          (0.75, _pluck(_hz("C5"), 0.95, sr, 0.6, 0.15))])


def _wake(sr: int) -> np.ndarray:
    seq = ("C5", "E5", "G5", "C6")
    return _mix(1.0, sr, [(i * 0.11, _pluck(_hz(note), 0.6 if i == 3 else 0.35, sr, 0.3 if i == 3 else 0.15))
                          for i, note in enumerate(seq)])


def _click(sr: int) -> np.ndarray:
    n = _n(0.08, sr)
    wood = _tone(900.0, n, sr, ((1, 1.0), (2.3, 0.2))) * _perc(n, sr, 0.006, 0.012)
    body = _tone(450.0, n, sr) * _perc(n, sr, 0.006, 0.02) * 0.4
    return wood + body


def _success(sr: int) -> np.ndarray:
    parts = [(0.0, _pluck(_hz("C5"), 0.3, sr, 0.12)), (0.08, _pluck(_hz("E5"), 0.3, sr, 0.12)),
             (0.16, _pluck(_hz("G5"), 0.3, sr, 0.12))]
    for note, amp in (("E5", 0.5), ("G5", 0.5), ("C6", 0.7)):
        parts.append((0.26, _bell(_hz(note), 0.85, sr, 0.45) * amp))
    return _mix(1.15, sr, parts)


def _listen(sr: int) -> np.ndarray:
    """"I'm listening" earcon (wake word heard), like a smart speaker: two quick, soft, rising
    tones a fifth apart (E5 -> B5), nearly pure sines, 0.27 s in all."""
    def tone(note: str, dur: float, tau: float) -> np.ndarray:
        n = _n(dur, sr)
        t = np.arange(n) / sr
        f = _hz(note)
        x = np.sin(TWO_PI * f * t) + 0.06 * np.sin(TWO_PI * 2 * f * t) * np.exp(-t / 0.04)
        return x * _attack(n, sr, 0.008) * np.exp(-t / tau)
    return _mix(0.27, sr, [(0.0, tone("E5", 0.12, 0.07) * 0.8), (0.09, tone("B5", 0.18, 0.08))])


_GENERATORS: dict[str, Callable[[int], np.ndarray]] = {
    "chime": _chime, "pop": _pop, "boop": _boop, "boing": _boing, "peekaboo": _peekaboo,
    "tada": _tada, "clap": _clap, "whoosh": _whoosh, "sparkle": _sparkle, "drum": _drum,
    "bell": _bell_sound, "giggle": _giggle, "uhoh": _uhoh, "yay": _yay, "blip": _blip,
    "notes": _notes, "sleep": _sleep, "wake": _wake, "click": _click, "success": _success,
    "listen": _listen,
}

# Sounds made mostly of noise get an extra low-pass pass.
_EXTRA_SOFT = {"clap": 2, "whoosh": 2, "drum": 2}


def synthesize(name: str, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Render one named effect (mono float32, peak about -6 dBFS)."""
    try:
        gen = _GENERATORS[name]
    except KeyError:
        raise KeyError(f"unknown sound {name!r}; known: {', '.join(SOUND_NAMES)}") from None
    return _master(gen(sample_rate), sample_rate, lowpass_passes=_EXTRA_SOFT.get(name, 1))


def synthesize_all(sample_rate: int = SAMPLE_RATE) -> dict[str, np.ndarray]:
    return {name: synthesize(name, sample_rate) for name in SOUND_NAMES}


# ================================================================ melodies
_NOTE_RE = re.compile(r"^([A-Ga-g])([#♯b♭]?)(-?\d+)?$")
_REST_TOKENS = {"r", "rest", "-", "_", "."}
_SEMITONES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
MIN_NOTE_HZ = 60.0  # below this small speakers only buzz
MAX_NOTE_HZ = 2200.0  # above ~C7 tones become shrill for little ears
MAX_MELODY_SECONDS = 60.0


def note_to_midi(note: str) -> int | None:
    """'C4' -> 60, 'F#4' -> 66, 'Bb3' -> 58, 'R' -> None (rest). Octave defaults to 4.

    Raises ValueError for anything that is not a note name or rest.
    """
    if not isinstance(note, str):
        raise ValueError(f"note must be text like 'C4', got {note!r}")
    token = note.strip()
    if token.lower() in _REST_TOKENS:
        return None
    m = _NOTE_RE.match(token)
    if not m:
        raise ValueError(f"invalid note {note!r} (expected e.g. 'C4', 'F#4', 'Bb3' or 'R')")
    letter, accidental, octave_s = m.groups()
    octave = int(octave_s) if octave_s is not None else 4
    if not 0 <= octave <= 8:
        raise ValueError(f"invalid octave in note {note!r}")
    semitone = _SEMITONES[letter.lower()]
    if accidental in ("#", "♯"):
        semitone += 1
    elif accidental in ("b", "♭"):
        semitone -= 1
    return 12 * (octave + 1) + semitone


def midi_to_hz(midi: int) -> float:
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def parse_note(note: str) -> float | None:
    """Frequency in Hz for a note name, None for a rest. ValueError if invalid."""
    midi = note_to_midi(note)
    return None if midi is None else midi_to_hz(midi)


def _normalise_notes(notes: Iterable[Any]) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    for item in notes:
        if isinstance(item, str):
            name, beats = item, 1.0
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            name, beats = item[0], item[1]
        else:
            raise ValueError(f"melody items must be 'C4' or ('C4', beats), got {item!r}")
        try:
            b = float(beats)
        except (TypeError, ValueError):
            raise ValueError(f"invalid beat length {beats!r} for note {name!r}") from None
        if not math.isfinite(b) or b <= 0 or b > 16:
            raise ValueError(f"beat length must be in (0, 16], got {beats!r}")
        out.append((name, b))
    return out


def _melody_note(freq: float, dur: float, sr: int, release: float) -> np.ndarray:
    total = dur + release
    n = _n(total, sr)
    t = np.arange(n) / sr
    x = np.sin(TWO_PI * freq * t)
    x += 0.22 * np.sin(TWO_PI * 2 * freq * t) * np.exp(-t / 0.25)
    x += 0.12 * np.sin(TWO_PI * 3 * freq * t) * np.exp(-t / 0.08)
    x += 0.05 * np.sin(TWO_PI * 4.02 * freq * t) * np.exp(-t / 0.04)
    # ADSR: 8 ms attack, 120 ms decay to a soft sustain, natural music-box fade, release tail
    env = _attack(n, sr, 0.008) * (0.38 + 0.62 * np.exp(-t / 0.12)) * np.exp(-t / (0.9 + 0.4 * dur))
    rel = np.clip((t - dur) / max(1e-3, release), 0.0, 1.0)
    env *= np.exp(-5.0 * rel) * (1.0 - rel)
    return x * env


def render_melody(notes: Iterable[Any], tempo_bpm: float = 100.0, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Render [("C4", 1), ("F#4", 0.5), ("R", 1), ("Bb3", 2)] as a soft music-box tune.

    Raises ValueError for invalid notes, beats, tempo, out-of-range pitches or
    melodies longer than ``MAX_MELODY_SECONDS``.
    """
    try:
        tempo = float(tempo_bpm)
    except (TypeError, ValueError):
        raise ValueError(f"invalid tempo {tempo_bpm!r}") from None
    if not math.isfinite(tempo) or not 20.0 <= tempo <= 300.0:
        raise ValueError(f"tempo must be between 20 and 300 bpm, got {tempo_bpm!r}")
    items = _normalise_notes(notes)
    if not items:
        raise ValueError("melody is empty")
    beat = 60.0 / tempo
    release = 0.22
    events: list[tuple[float, float, float]] = []  # (start, freq, dur)
    pos = 0.0
    for name, beats in items:
        freq = parse_note(name)
        dur = beats * beat
        if freq is not None:
            if not MIN_NOTE_HZ <= freq <= MAX_NOTE_HZ:
                raise ValueError(f"note {name!r} ({freq:.0f} Hz) is outside the gentle range (about B1..C7)")
            events.append((pos, freq, dur))
        pos += dur
    if pos > MAX_MELODY_SECONDS:
        raise ValueError(f"melody is too long ({pos:.1f}s > {MAX_MELODY_SECONDS:.0f}s)")
    total = pos + release + 0.05
    if not events:
        return np.zeros(_n(min(total, MAX_MELODY_SECONDS), sample_rate), dtype=np.float32)
    mix = _mix(total, sample_rate, [(start, _melody_note(freq, dur, sample_rate, release)) for start, freq, dur in events])
    return _master(mix, sample_rate)


# ================================================================ sound bank
class SoundBank:
    """Loads ``assets/sounds/*.wav`` on demand (cached) and synthesises missing built-ins."""

    def __init__(self, sounds_dir: str | Path | None = None, *, synthesize_missing: bool = True,
                 cache_size: int = 48, user_dir: str | Path | None = None) -> None:
        self.sounds_dir = Path(sounds_dir) if sounds_dir is not None else SOUNDS_DIR
        # The parent's own recordings (data/sounds/voice/<name>_N.wav, e.g. from scripts/record_giggle.py):
        # outside the code folder, so updates never touch them; they win over the shipped clips.
        self.user_dir = Path(user_dir) if user_dir is not None else None
        self.synthesize_missing = synthesize_missing
        self._cache: OrderedDict[str, tuple[np.ndarray, int]] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self._variants: dict[str, list[str]] = {}

    def names(self) -> list[str]:
        extra = []
        if self.sounds_dir.is_dir():
            extra = sorted(p.stem for p in self.sounds_dir.glob("*.wav") if p.stem not in SOUND_NAMES)
        return list(SOUND_NAMES) + extra

    def cached(self, name: str) -> tuple[np.ndarray, int] | None:
        with self._lock:
            hit = self._cache.get(name)
            if hit is not None:
                self._cache.move_to_end(name)
            return hit

    def resolve_path(self, name: str) -> Path | None:
        """Safe path for a sound name or a relative .wav path under the sounds dir."""
        if not name or "\x00" in name:
            return None
        rel = name if name.lower().endswith(".wav") else f"{name}.wav"
        root = self.sounds_dir.resolve()
        try:
            path = (root / rel).resolve()
            path.relative_to(root)  # refuses ../ escapes and absolute paths
        except (ValueError, OSError):
            return None
        return path

    def voice_variants(self, name: str) -> list[str]:
        """Recorded voice clips that replace a built-in sound (assets/sounds/voice/<name>_N.wav)."""
        if name not in SOUND_NAMES:
            return []
        found = self._variants.get(name)
        if found is None:
            found = []
            mine = self.user_dir / "voice" if self.user_dir is not None else None
            if mine is not None and mine.is_dir():
                found = [str(p) for p in sorted(mine.glob(f"{name}_*.wav"))]
            if found:
                log.info("sound '%s': using your %d recording(s) in %s", name, len(found), mine)
            else:
                folder = self.sounds_dir / "voice"
                found = [f"voice/{p.name}" for p in sorted(folder.glob(f"{name}_*.wav"))] if folder.is_dir() else []
            self._variants[name] = found
        return found

    def _load_user_clip(self, path: str) -> tuple[np.ndarray, int] | None:
        hit = self.cached(path)
        if hit is not None:
            return hit
        try:
            result = dsp.read_wav(Path(path))
        except (OSError, ValueError) as e:
            log.warning("could not read sound %s: %s", path, e)
            return None
        with self._lock:
            self._cache[path] = result
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result

    def get(self, name: str) -> tuple[np.ndarray, int] | None:
        """(samples, sample_rate) or None if unknown / unreadable. A built-in with recorded voice
        clips (e.g. a human "Hehe! That tickles!" for "giggle") plays one of them at random."""
        variants = self.voice_variants(name)
        if variants:
            pick = random.choice(variants)
            clip = self._load_user_clip(pick) if Path(pick).is_absolute() else self.get(pick)
            if clip is not None:
                return clip
        hit = self.cached(name)
        if hit is not None:
            return hit
        result: tuple[np.ndarray, int] | None = None
        path = self.resolve_path(name)
        if path is not None and path.is_file():
            try:
                result = dsp.read_wav(path)
            except (OSError, ValueError) as e:
                log.warning("could not read sound %s: %s", path, e)
        if result is None and name in SOUND_NAMES and self.synthesize_missing:
            result = (synthesize(name), SAMPLE_RATE)
        if result is None:
            return None
        with self._lock:
            self._cache[name] = result
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result
