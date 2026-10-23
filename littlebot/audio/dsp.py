"""Small numpy-only signal helpers shared by the audio package.

All audio inside LittleBot is handled as mono ``float32`` arrays in [-1, 1]
plus an integer sample rate. Conversion to device formats (int16, stereo,
other sample rates) happens at the edges (device backends, WAV files).
"""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

# Above this many samples, FFT resampling is replaced by linear interpolation
# (keeps peak memory low on the 2 GB Pi for very long buffers).
_FFT_RESAMPLE_MAX = 1_500_000


# ---------------------------------------------------------------- basics
def as_mono_float(samples: np.ndarray) -> np.ndarray:
    """Any int/float array, mono or (frames, channels), -> float32 mono in [-1, 1]."""
    x = np.asarray(samples)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype == np.uint8:
        x = (x.astype(np.float32) - 128.0) / 128.0
    else:
        x = x.astype(np.float32, copy=False)
    if x.ndim == 2:
        x = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    elif x.ndim != 1:
        x = x.reshape(-1)
    return np.ascontiguousarray(x, dtype=np.float32)


def to_int16(samples: np.ndarray) -> np.ndarray:
    x = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return np.round(x * 32767.0).astype(np.int16)


def peak(samples: np.ndarray) -> float:
    return float(np.max(np.abs(samples))) if len(samples) else 0.0


def rms(samples: np.ndarray) -> float:
    if not len(samples):
        return 0.0
    x = np.asarray(samples, dtype=np.float64)
    return float(math.sqrt(float(np.mean(x * x))))


def dbfs(value: float) -> float:
    return 20.0 * math.log10(value) if value > 1e-9 else -180.0


def duration(samples: np.ndarray, sample_rate: int) -> float:
    return len(samples) / float(sample_rate) if sample_rate else 0.0


# --------------------------------------------------------------- shaping
def fade(samples: np.ndarray, sample_rate: int, fade_in: float = 0.005, fade_out: float = 0.01) -> np.ndarray:
    """Raised-cosine fade in/out (removes clicks at the edges)."""
    x = np.array(samples, dtype=np.float32, copy=True)
    n = len(x)
    for secs, rising in ((fade_in, True), (fade_out, False)):
        k = min(n, int(round(secs * sample_rate)))
        if k <= 1:
            continue
        ramp = (0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, k))).astype(np.float32)
        if rising:
            x[:k] *= ramp
        else:
            x[n - k:] *= ramp[::-1]
    return x


def soft_lowpass(samples: np.ndarray, passes: int = 1) -> np.ndarray:
    """Gentle 3-tap [1/4, 1/2, 1/4] low-pass (zero at Nyquist): softens buzzy synthetic speech."""
    x = np.asarray(samples, dtype=np.float32)
    if len(x) < 3:
        return x.copy()
    kernel = np.array([0.25, 0.5, 0.25], dtype=np.float32)
    for _ in range(max(1, passes)):
        x = np.convolve(x, kernel, mode="same").astype(np.float32)
    return x


def moving_average(samples: np.ndarray, width: int) -> np.ndarray:
    width = max(1, int(width))
    if width == 1 or len(samples) < width:
        return np.asarray(samples, dtype=np.float32).copy()
    kernel = np.full(width, 1.0 / width, dtype=np.float32)
    return np.convolve(np.asarray(samples, dtype=np.float32), kernel, mode="same").astype(np.float32)


def normalize_peak(samples: np.ndarray, target: float) -> np.ndarray:
    p = peak(samples)
    if p < 1e-9:
        return np.asarray(samples, dtype=np.float32).copy()
    return (np.asarray(samples, dtype=np.float32) * (target / p)).astype(np.float32)


def pad(samples: np.ndarray, sample_rate: int, before: float = 0.0, after: float = 0.0) -> np.ndarray:
    a = np.zeros(int(before * sample_rate), dtype=np.float32)
    b = np.zeros(int(after * sample_rate), dtype=np.float32)
    return np.concatenate([a, np.asarray(samples, dtype=np.float32), b])


def prepare_speech(samples: np.ndarray, sample_rate: int, target_peak: float = 0.6) -> np.ndarray:
    """Post-process TTS output: soften, de-click, add short silences, consistent loudness."""
    x = as_mono_float(samples)
    x = x - float(np.mean(x)) if len(x) else x  # remove DC offset
    x = soft_lowpass(x)
    x = fade(x, sample_rate, 0.008, 0.02)
    x = normalize_peak(x, target_peak)
    return pad(x, sample_rate, before=0.04, after=0.08)


# ------------------------------------------------------------ resampling
def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Band-limited (FFT) resampling for whole buffers; linear for very long ones."""
    x = np.asarray(samples, dtype=np.float32)
    if src_rate == dst_rate or len(x) == 0:
        return x.copy()
    n_out = max(1, int(round(len(x) * dst_rate / float(src_rate))))
    if len(x) > _FFT_RESAMPLE_MAX:
        return resample_linear(x, src_rate, dst_rate)
    spec = np.fft.rfft(x.astype(np.float64))
    n_bins = n_out // 2 + 1
    if n_bins <= len(spec):
        spec = spec[:n_bins]
    else:
        spec = np.concatenate([spec, np.zeros(n_bins - len(spec), dtype=spec.dtype)])
    y = np.fft.irfft(spec, n_out) * (n_out / float(len(x)))
    return y.astype(np.float32)


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap streaming-friendly resampler (box-car anti-alias when downsampling)."""
    x = np.asarray(samples, dtype=np.float32)
    if src_rate == dst_rate or len(x) == 0:
        return x.copy()
    if dst_rate < src_rate:
        x = moving_average(x, int(round(src_rate / float(dst_rate))))
    n_out = max(1, int(round(len(x) * dst_rate / float(src_rate))))
    pos = np.arange(n_out, dtype=np.float64) * (src_rate / float(dst_rate))
    return np.interp(pos, np.arange(len(x), dtype=np.float64), x).astype(np.float32)


class StreamResampler:
    """Chunk-by-chunk linear resampler that keeps phase across chunks (microphone path).

    Downsampling uses a causal box-car anti-alias filter whose state also
    carries across chunks, so chunk boundaries do not click.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate, self.dst_rate = int(src_rate), int(dst_rate)
        self.ratio = self.src_rate / float(self.dst_rate)
        width = int(round(self.ratio)) if self.dst_rate < self.src_rate else 1
        self._kernel = np.full(width, 1.0 / width, dtype=np.float32) if width > 1 else None
        self._tail = np.zeros(max(0, width - 1), dtype=np.float32)
        self._last: float | None = None
        self._t = 0.0  # position of the next output sample, relative to self._last

    def process(self, chunk: np.ndarray) -> np.ndarray:
        x = np.asarray(chunk, dtype=np.float32)
        if self.src_rate == self.dst_rate or len(x) == 0:
            return x.copy()
        if self._kernel is not None:
            ext = np.concatenate([self._tail, x])
            self._tail = ext[len(ext) - len(self._tail):].copy() if len(self._tail) else self._tail
            x = np.convolve(ext, self._kernel, mode="valid").astype(np.float32)
        if self._last is None:
            buf, t = x, 0.0
        else:
            buf, t = np.concatenate([[self._last], x]).astype(np.float32), self._t
        end = len(buf) - 1
        if t > end:
            self._t = t - end
            self._last = float(buf[-1])
            return np.zeros(0, dtype=np.float32)
        positions = np.arange(t, end + 1e-9, self.ratio)
        out = np.interp(positions, np.arange(len(buf), dtype=np.float64), buf).astype(np.float32)
        self._t = float(positions[-1] + self.ratio - end)
        self._last = float(buf[-1])
        return out


# ---------------------------------------------------------- level envelope
def level_envelope(samples: np.ndarray, sample_rate: int, hz: float = 16.0) -> np.ndarray:
    """Per-window loudness 0..1 for mouth/eye animation (independent of volume)."""
    x = np.asarray(samples, dtype=np.float32)
    hop = max(1, int(sample_rate / hz))
    n_win = int(math.ceil(len(x) / hop)) if len(x) else 0
    if n_win == 0:
        return np.zeros(0, dtype=np.float32)
    padded = np.zeros(n_win * hop, dtype=np.float32)
    padded[: len(x)] = x
    frames = padded.reshape(n_win, hop).astype(np.float64)
    levels = np.sqrt(np.mean(frames * frames, axis=1))
    ref = float(np.percentile(levels, 95)) if n_win > 1 else float(levels.max())
    if ref < 1e-6:
        return np.zeros(n_win, dtype=np.float32)
    levels = np.clip(levels / ref, 0.0, 1.0) ** 0.6
    levels[levels < 0.08] = 0.0  # gate: silence closes the mouth
    # light smoothing so the mouth does not flicker
    if n_win > 2:
        levels = np.convolve(levels, [0.25, 0.5, 0.25], mode="same")
    return np.clip(levels, 0.0, 1.0).astype(np.float32)


# ------------------------------------------------------------------ visemes
# Mouth shapes the face shows while LittleBot talks, estimated from the sound itself (no phoneme
# timing from the TTS needed, so it works for every voice, local or cloud, and for live calls):
#   closed - silence, or lips pressed together (the "mmm" of m / b / p)
#   small  - slightly open: most consonants (t, d, k, l, n, h ...) and weak vowels ("uh")
#   ah     - wide open: open vowels (a in "father", the start of "I")
#   ee     - wide, teeth showing: front vowels (ee, eh) and hissing sounds (s, z, sh, f, ch)
#   oo     - round: rounded vowels and w (oo, oh, w)
VISEMES: tuple[str, ...] = ("closed", "small", "ah", "ee", "oo")
VISEME_HZ = 50.0  # one mouth frame per 20 ms

# How (per 20 ms block; ~0.2 ms each on a Pi 5):
# * Vowels by their first three formants F1-F3, estimated with LPC (order 10 on an 8 kHz copy):
#   a high F1 = open jaw ("ah"); a high F2 far above F1 = spread lips ("ee", "eh"); a low F2,
#   or a fronted "oo" with a lowered F3 = rounded lips ("oo", "oh", "w"). Formants do not depend
#   on how bright or dull a voice is, so this works the same for espeak-ng, neural TTS and the
#   live ChatGPT voice (tested on espeak voices and on synthetic adult / child vowels).
# * Hissing (s, sh, f) by its energy above 3.8 kHz or a high zero-crossing rate.
# * A hum (m) by being dull (little energy above 1.6 kHz) and very low: a very low F1 with no
#   real F2 above it, or most of the energy below the first formant LPC finds (a nasal murmur).
_VISEME_GATE = 0.1  # relative level below which the mouth is closed (silence between words)
_VISEME_MIN_RMS = 1e-3  # anything quieter than -60 dBFS is silence, whatever the reference
_HISS_HF, _HISS_ZCR = 0.3, 5000.0  # share of energy above 3.8 kHz / zero crossings per second
_HUM_F2_DB = -8.0  # a hum is dull: 1.6-3.6 kHz this far below 0.5-1.6 kHz
_HUM_F1, _HUM_F2, _HUM_LEVEL = 300.0, 1400.0, 0.7  # ... quieter than vowels, F1 below, no F2 above
_MURMUR_CW, _MURMUR_RATIO = 480.0, 2.0  # ... or energy centroid below this and F1 above ratio x centroid
_OPEN_F1 = 620.0  # Hz
_QUIET_LEVEL = 0.3  # an open vowel this quiet (onsets, endings) is only a small opening
_FRONT_F2, _FRONT_SPREAD, _FRONT_F3_RATIO = 1800.0, 1200.0, 1.45  # F2 min, F2-F1 min, F3/F2 max
_ROUND_F1, _ROUND_F2 = 560.0, 1250.0  # back rounded vowels: F1 below, F2 below
_FRONTED_ROUND = (450.0, 2000.0, 2450.0)  # fronted "oo" (modern English): F1, F2, F3 below
_HUM_LEVEL_CAP = 0.25  # level reported with a hum: closed lips, even for level-only listeners
_LPC_RATE, _LPC_ORDER = 8000, 10
_LPC_NFFT = 512
_LPC_LAG = np.exp(-0.5 * (2.0 * np.pi * 60.0 / _LPC_RATE * np.arange(_LPC_ORDER + 1)) ** 2)  # 60 Hz bw expansion
_LPC_FREQS = np.fft.rfftfreq(_LPC_NFFT, 1.0 / _LPC_RATE)
_LPC_PEAK_BAND = (_LPC_FREQS[1:-1] > 180.0) & (_LPC_FREQS[1:-1] < _LPC_RATE / 2.0 - 200.0)
_PEAK_SPAN, _PEAK_MIN = 10, 0.23  # a formant stands 1 dB (0.23 neper) above the envelope +-156 Hz away
_NO_FORMANT = 4000.0
_BAND_EDGES_HZ = (80, 150, 500, 1000, 1600, 3600, 3800, 4000)
_BAND_CACHE: dict[tuple[int, int], tuple[Any, ...]] = {}
_HAMMING: dict[int, np.ndarray] = {}


class VisemeFeatures(NamedTuple):
    """What the viseme classifier looks at for one 20 ms block (see the notes above)."""

    rms: float  # loudness (absolute)
    f2db: float  # energy 1.6-3.6 kHz vs 0.5-1.6 kHz, dB
    cw: float  # spectral centroid of 150-4000 Hz, Hz
    low: float  # share of 80-4000 Hz energy below 1 kHz
    hf: float  # share of energy above 3.8 kHz
    zcr: float  # zero crossings per second
    f1: float  # formants (Hz) from LPC; 4000 when not found
    f2: float
    f3: float


def _band_plan(hop: int, sample_rate: int) -> tuple[Any, ...]:
    """FFT size, window, bin frequencies and band edge bins for ``hop``-sample frames (cached)."""
    key = (int(hop), int(sample_rate))
    plan = _BAND_CACHE.get(key)
    if plan is None:
        nfft = 1 << max(1, (int(hop) - 1).bit_length())
        freqs = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
        nyq = sample_rate / 2.0 + 1.0
        edges = {hz: int(np.searchsorted(freqs, min(float(hz), nyq))) for hz in _BAND_EDGES_HZ}
        plan = (nfft, np.hanning(hop), freqs, edges)
        if len(_BAND_CACHE) > 16:
            _BAND_CACHE.clear()
        _BAND_CACHE[key] = plan
    return plan


def _hamming(n: int) -> np.ndarray:
    w = _HAMMING.get(n)
    if w is None:
        w = _HAMMING[n] = np.hamming(n)
    return w


def _band_features(frames: np.ndarray, sample_rate: int) -> list[np.ndarray]:
    """rms, f2db, cw, low, hf, zcr for each row of ``frames`` (float64, shape ``(n, hop)``)."""
    n, hop = frames.shape
    nfft, win, freqs, e = _band_plan(hop, sample_rate)
    spec = np.fft.rfft(frames * win, nfft, axis=1)
    p = spec.real * spec.real + spec.imag * spec.imag
    cp = np.zeros((n, p.shape[1] + 1))  # band sums from cumulative sums: O(1) per band
    cf = np.zeros((n, p.shape[1] + 1))
    np.cumsum(p, axis=1, out=cp[:, 1:])
    np.cumsum(p * freqs, axis=1, out=cf[:, 1:])
    total = cp[:, -1]

    def energy(lo: int, hi: int) -> np.ndarray:
        return cp[:, e[hi]] - cp[:, e[lo]] + 1e-12

    signs = np.signbit(frames)
    crossings = np.count_nonzero(signs[:, 1:] != signs[:, :-1], axis=1)
    return [
        np.sqrt(np.mean(frames * frames, axis=1)),
        10.0 * np.log10(energy(1600, 3600) / energy(500, 1600)),
        (cf[:, e[4000]] - cf[:, e[150]]) / energy(150, 4000),
        energy(80, 1000) / energy(80, 4000),
        (total - cp[:, e[3800]]) / (total - cp[:, e[80]] + 1e-12),
        crossings * (sample_rate / float(hop)),
    ]


def _block_band_features(x: np.ndarray, sample_rate: int) -> tuple[float, ...]:
    """:func:`_band_features` of one 1-D float64 block as plain floats (lean: the live audio callback)."""
    nfft, win, freqs, e = _band_plan(len(x), sample_rate)
    spec = np.fft.rfft(x * win, nfft)
    p = spec.real * spec.real + spec.imag * spec.imag
    cp = [0.0] + np.cumsum(p).tolist()
    cf = [0.0] + np.cumsum(p * freqs).tolist()
    signs = np.signbit(x)
    crossings = int(np.count_nonzero(signs[1:] != signs[:-1]))

    def energy(lo: int, hi: int) -> float:
        return cp[e[hi]] - cp[e[lo]] + 1e-12

    total = cp[-1]
    return (
        math.sqrt(float(np.dot(x, x)) / len(x)),
        10.0 * math.log10(energy(1600, 3600) / energy(500, 1600)),
        (cf[e[4000]] - cf[e[150]]) / energy(150, 4000),
        energy(80, 1000) / energy(80, 4000),
        (total - cp[e[3800]]) / (total - cp[e[80]] + 1e-12),
        crossings * (sample_rate / float(len(x))),
    )


def lpc_formants(frames: np.ndarray, sample_rate: int = _LPC_RATE) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """First three formant frequencies (Hz) of each row of ``frames`` (ideally ~8 kHz audio).

    Pre-emphasis, Hamming window, autocorrelation LPC (Levinson-Durbin, vectorised over the
    frames) and the peaks of the LPC envelope. A formant that is not found reads 4000 Hz.
    """
    x = np.asarray(frames, dtype=np.float64)
    if x.ndim == 1:
        x = x[None, :]
    n, m = x.shape
    order = _LPC_ORDER
    x = x - x.mean(axis=1, keepdims=True)
    x = np.concatenate([x[:, :1], x[:, 1:] - 0.97 * x[:, :-1]], axis=1) * _hamming(m)
    r = np.stack([np.einsum("ij,ij->i", x[:, : m - k], x[:, k:]) for k in range(order + 1)], axis=1)
    r[:, 0] = r[:, 0] * 1.001 + 1e-12  # a touch of white noise keeps silent frames stable
    lag = _LPC_LAG if sample_rate == _LPC_RATE else np.exp(
        -0.5 * (2.0 * np.pi * 60.0 / sample_rate * np.arange(order + 1)) ** 2)
    r = r * lag
    a = np.zeros((n, order + 1))
    a[:, 0] = 1.0
    err = r[:, 0].copy()
    for i in range(1, order + 1):
        acc = r[:, i] + np.einsum("ij,ij->i", a[:, 1:i], r[:, i - 1:0:-1])
        k = -acc / err
        prev = a[:, 1:i].copy()
        a[:, 1:i] = prev + k[:, None] * prev[:, ::-1]
        a[:, i] = k
        err = err * (1.0 - k * k)
    spec = np.fft.rfft(a, _LPC_NFFT, axis=1)
    env = -np.log(spec.real * spec.real + spec.imag * spec.imag + 1e-12)
    freqs = np.fft.rfftfreq(_LPC_NFFT, 1.0 / sample_rate)
    band = (freqs[1:-1] > 180.0) & (freqs[1:-1] < sample_rate / 2.0 - 200.0)
    d = _PEAK_SPAN  # ignore bumps: LPC sometimes spends a pole on a pitch harmonic between formants
    left = np.concatenate([np.repeat(env[:, :1], d, axis=1), env[:, :-d]], axis=1)
    right = np.concatenate([env[:, d:], np.repeat(env[:, -1:], d, axis=1)], axis=1)
    prominent = env - np.maximum(left, right) > _PEAK_MIN
    peaks = (env[:, 1:-1] > env[:, :-2]) & (env[:, 1:-1] >= env[:, 2:]) & band & prominent[:, 1:-1]
    rows = np.arange(n)
    out = []
    for _ in range(3):
        i = np.argmax(peaks, axis=1)
        found = peaks[rows, i]
        out.append(np.where(found, freqs[1:-1][i], _NO_FORMANT))
        peaks[rows, i] = False
    return out[0], out[1], out[2]


def _lpc_formants_block(x: np.ndarray) -> tuple[float, float, float]:
    """:func:`lpc_formants` of one 8 kHz block, lean (plain-Python Levinson): the live audio callback."""
    m = len(x)
    order = _LPC_ORDER
    x = x - float(x.sum()) / m
    y = np.empty(m)
    y[0] = x[0]
    np.subtract(x[1:], 0.97 * x[:-1], out=y[1:])
    y *= _hamming(m)
    lag = _LPC_LAG.tolist()
    r = [float(np.dot(y[: m - k], y[k:])) * lag[k] for k in range(order + 1)]
    r[0] = r[0] * 1.001 + 1e-12
    a = [1.0] + [0.0] * order
    err = r[0]
    for i in range(1, order + 1):
        k = -(r[i] + sum(a[j] * r[i - j] for j in range(1, i))) / err
        prev = a[:]
        for j in range(1, i):
            a[j] = prev[j] + k * prev[i - j]
        a[i] = k
        err *= 1.0 - k * k
    spec = np.fft.rfft(np.asarray(a), _LPC_NFFT)
    env = -np.log(spec.real * spec.real + spec.imag * spec.imag + 1e-12)
    d = _PEAK_SPAN
    side = np.maximum(np.concatenate([np.full(d, env[0]), env[:-d]]), np.concatenate([env[d:], np.full(d, env[-1])]))
    peaks = np.flatnonzero((env[1:-1] > env[:-2]) & (env[1:-1] >= env[2:]) & _LPC_PEAK_BAND
                           & (env[1:-1] - side[1:-1] > _PEAK_MIN))[:3]
    f = [float(_LPC_FREQS[1:-1][i]) for i in peaks] + [_NO_FORMANT] * 3
    return f[0], f[1], f[2]


def _to_lpc_rate(x: np.ndarray, sample_rate: int) -> np.ndarray:
    """Cheap decimation to 8 kHz for LPC (formants are below 4 kHz); the same for blocks and tracks."""
    if sample_rate <= _LPC_RATE:
        return x
    step = sample_rate / _LPC_RATE
    if step == int(step) and len(x) % int(step) == 0:  # 16 / 48 kHz: average groups (box-car anti-alias)
        return x.reshape(-1, int(step)).mean(axis=1)
    return resample_linear(x, sample_rate, _LPC_RATE).astype(np.float64)


def viseme_features(frames: np.ndarray, sample_rate: int) -> list[VisemeFeatures]:
    """:class:`VisemeFeatures` of each row of ``frames`` (shape ``(n, hop)``, or one 1-D block)."""
    x = np.asarray(frames, dtype=np.float64)
    if x.ndim == 1:
        x = x[None, :]
    lpc_rate = min(sample_rate, _LPC_RATE)
    low = np.stack([_to_lpc_rate(row, sample_rate) for row in x]) if sample_rate > _LPC_RATE else x
    cols = _band_features(x, sample_rate) + list(lpc_formants(low, lpc_rate))
    return [VisemeFeatures(*row) for row in zip(*(c.tolist() for c in cols))]


def classify_viseme(level: float, f: VisemeFeatures) -> str:
    """One of :data:`VISEMES` for a block's relative level (0..1) and its :class:`VisemeFeatures`."""
    if level < _VISEME_GATE or f.rms < _VISEME_MIN_RMS:
        return "closed"  # silence
    if f.hf > _HISS_HF or f.zcr > _HISS_ZCR:
        return "ee"  # s, z, sh, f: teeth
    no_f2 = f.f2 >= _NO_FORMANT
    if f.f2db < _HUM_F2_DB and ((f.f1 < _HUM_F1 and (f.f2 < _HUM_F2 or no_f2) and level < _HUM_LEVEL)
                                or (f.cw < _MURMUR_CW and f.f1 > _MURMUR_RATIO * f.cw)):
        return "closed"  # m: lips together
    if f.f1 > _OPEN_F1 and level >= _QUIET_LEVEL:
        return "ah"
    if not no_f2 and f.f2 > _FRONT_F2 and f.f2 - f.f1 > _FRONT_SPREAD \
            and (f.f3 < _FRONT_F3_RATIO * f.f2 or f.f3 >= _NO_FORMANT):
        return "ee"  # (a rounded "oo" said far forward keeps F3 close to F2; a missing F3 is no evidence)
    if f.f1 < _ROUND_F1 and (f.f2 < _ROUND_F2 or (f.f1 < _FRONTED_ROUND[0] and f.f2 < _FRONTED_ROUND[1]
                                                  and f.f3 < _FRONTED_ROUND[2])):
        return "oo"
    return "small"


def _despeckle(shapes: list[str]) -> list[str]:
    """Drop one-frame blips (a b a -> a a a): a steadier mouth; real changes last 40 ms or more."""
    out = list(shapes)
    for i in range(1, len(out) - 1):
        if out[i - 1] == shapes[i + 1] != shapes[i]:
            out[i] = out[i - 1]
    return out


def viseme_track(samples: np.ndarray, sample_rate: int, hz: float = VISEME_HZ) -> tuple[np.ndarray, tuple[str, ...]]:
    """Mouth level 0..1 and shape for every ``1/hz`` s of a whole utterance (precomputed for TTS).

    The level is the volume-independent :func:`level_envelope`; the shape is one of
    :data:`VISEMES`. Silent frames are ``(0.0, "closed")``. ~3 ms per second of audio.
    """
    x = np.asarray(samples, dtype=np.float32)
    levels = level_envelope(x, sample_rate, hz)
    n = len(levels)
    if n == 0:
        return levels, ()
    hop = max(1, int(sample_rate / hz))
    padded = np.zeros(n * hop, dtype=np.float64)
    padded[: len(x)] = x[: n * hop]
    band = _band_features(padded.reshape(n, hop), sample_rate)
    lpc_rate = min(sample_rate, _LPC_RATE)
    lo = _to_lpc_rate(padded, sample_rate)
    lhop = max(1, int(lpc_rate / hz))
    lo = np.concatenate([lo, np.zeros(max(0, n * lhop - len(lo)))])[: n * lhop]
    formants = lpc_formants(lo.reshape(n, lhop), lpc_rate)
    cols = [c.tolist() for c in band + list(formants)]
    shapes = _despeckle([classify_viseme(lv, VisemeFeatures(*row))
                         for lv, row in zip(levels.tolist(), zip(*cols))])
    out = levels.copy()
    rms = cols[0]
    for i, shape in enumerate(shapes):
        if out[i] < _VISEME_GATE or rms[i] < _VISEME_MIN_RMS:
            out[i], shapes[i] = 0.0, "closed"
        elif shape == "closed":
            out[i] = min(float(out[i]), _HUM_LEVEL_CAP)
    return out.astype(np.float32), tuple(shapes)


class VisemeEstimator:
    """Streaming viseme estimation, one block at a time (live voice: the block being played).

    The level is relative to a running peak (instant attack, ``release_seconds`` decay, never
    below ``floor``), so it does not depend on how loud the voice arrives.
    """

    def __init__(self, sample_rate: int, *, release_seconds: float = 2.5, floor: float = 0.01) -> None:
        self.sample_rate = int(sample_rate)
        self.release_seconds = max(0.1, float(release_seconds))
        self.floor = max(1e-4, float(floor))
        self.ref = self.floor
        self.level = 0.0
        self.shape = "closed"

    def reset(self) -> None:
        self.ref, self.level, self.shape = self.floor, 0.0, "closed"

    def features(self, x: np.ndarray) -> VisemeFeatures:
        """Features of one block (float64), computed lean for the audio callback."""
        return VisemeFeatures(*_block_band_features(x, self.sample_rate),
                              *_lpc_formants_block(_to_lpc_rate(x, self.sample_rate)))

    def process(self, block: np.ndarray) -> tuple[float, str]:
        """``(level 0..1, shape)`` for this block of mono float audio (any length >= 16 samples)."""
        x = np.asarray(block, dtype=np.float64).reshape(-1)
        if len(x) < 16:
            return self.level, self.shape
        rms = math.sqrt(float(np.dot(x, x)) / len(x))
        decay = math.exp(-len(x) / (self.sample_rate * self.release_seconds))
        self.ref = max(self.floor, rms, self.ref * decay)
        level = min(1.0, rms / self.ref) ** 0.6 if rms >= _VISEME_MIN_RMS else 0.0
        if level < _VISEME_GATE:
            self.level, self.shape = 0.0, "closed"
            return self.level, self.shape
        self.shape = classify_viseme(level, self.features(x))
        self.level = min(level, _HUM_LEVEL_CAP) if self.shape == "closed" else level
        return self.level, self.shape


# ------------------------------------------------------------------- WAV I/O
def encode_wav(samples: np.ndarray, sample_rate: int) -> bytes:
    """Mono 16-bit PCM WAV, in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(to_int16(samples).tobytes())
    return buf.getvalue()


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Parse a RIFF/WAVE buffer -> (float32 mono, sample_rate).

    Tolerates *streaming* headers (data size 0 or 0xFFFFFFFF, as written by
    ``espeak-ng --stdout`` and HTTP TTS APIs), PCM 8/16/24/32-bit and float32.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE buffer")
    pos = 12
    fmt: tuple[int, int, int, int] | None = None
    payload: bytes | None = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body_start = pos + 8
        if cid == b"fmt ":
            audio_format, channels, rate = struct.unpack("<HHI", data[body_start:body_start + 8])
            bits = struct.unpack("<H", data[body_start + 14:body_start + 16])[0]
            if audio_format == 0xFFFE and size >= 26:  # WAVE_FORMAT_EXTENSIBLE: real format in the sub-GUID
                audio_format = struct.unpack("<H", data[body_start + 24:body_start + 26])[0]
            fmt = (audio_format, channels, rate, bits)
        elif cid == b"data":
            end = body_start + size
            if size in (0, 0xFFFFFFFF) or end > len(data):
                end = len(data)
            payload = data[body_start:end]
            break
        pos = body_start + size + (size & 1)
    if fmt is None or payload is None:
        raise ValueError("WAV buffer has no fmt/data chunk")
    audio_format, channels, rate, bits = fmt
    channels = max(1, channels)
    if audio_format == 3 and bits == 32:
        arr = np.frombuffer(payload[: len(payload) // 4 * 4], dtype="<f4").astype(np.float32)
    elif audio_format == 1 and bits == 16:
        arr = np.frombuffer(payload[: len(payload) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    elif audio_format == 1 and bits == 8:
        arr = (np.frombuffer(payload, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif audio_format == 1 and bits == 24:
        raw = np.frombuffer(payload[: len(payload) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        arr = ints.astype(np.float32) / 8388608.0
    elif audio_format == 1 and bits == 32:
        arr = np.frombuffer(payload[: len(payload) // 4 * 4], dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV format {audio_format} / {bits} bit")
    frames = len(arr) // channels
    arr = arr[: frames * channels]
    if channels > 1:
        arr = arr.reshape(frames, channels).mean(axis=1)
    return np.ascontiguousarray(arr, dtype=np.float32), int(rate)


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    return decode_wav(Path(path).read_bytes())


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(encode_wav(samples, sample_rate))
