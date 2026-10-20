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
