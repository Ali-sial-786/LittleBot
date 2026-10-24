"""The speaker-embedding model (sherpa-onnx) and the small sound helpers around it.

``SpeakerEngine`` turns a stretch of speech (16 kHz mono float) into a voiceprint: 192 numbers (for
the default model, 3D-Speaker's ERes2Net trained on VoxCeleb) that describe how someone sounds, not what
they said. Nothing here keeps any sound: every function takes the samples it is given and returns
numbers or a new array; the caller drops the samples afterwards.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from littlebot.audio import dsp

log = logging.getLogger("littlebot.voices")

RATE = 16000  # what the model takes
FRAME_S = 0.02  # speech is picked out 20 ms at a time
MIN_LEVEL = 0.004  # RMS (full scale 1.0) below which a frame is never speech
REL_LEVEL = 0.05  # ...nor when quieter than this share of the turn's loud frames (the 95th percentile, -26 dB)
HANGOVER_FRAMES = 4  # quiet frames this close to speech still count (soft consonants, the ends of words)
MAX_EMBED_S = 3.0  # at most this much speech per voiceprint: memory grows with the input (~+20 MB per s)


class EngineUnavailable(Exception):
    """The model can't run. ``reason``: no_package | no_model | load_failed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sherpa_installed() -> bool:
    try:
        import sherpa_onnx  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001 - ImportError, or a broken binary wheel
        return False
    return True


class SpeakerEngine:
    """sherpa-onnx's ``SpeakerEmbeddingExtractor`` for one model file (thread-safe: one voiceprint at a time)."""

    def __init__(self, model: str | Path, *, threads: int = 2) -> None:
        try:
            import sherpa_onnx  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            raise EngineUnavailable("no_package") from None
        path = Path(model)
        if not path.is_file():
            raise EngineUnavailable("no_model")
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=max(1, int(threads)),
                                                             provider="cpu")
        if not config.validate():  # sherpa-onnx may exit the process on a config it can't use: check first
            raise EngineUnavailable("load_failed")
        try:
            self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        except Exception:  # noqa: BLE001 - RuntimeError: a broken or wrong model file
            raise EngineUnavailable("load_failed") from None
        self.dim = int(self._ex.dim)
        self.name = path.name
        self._lock = threading.Lock()
        # A broken model fails here (the dashboard then says so), not in the middle of a call.
        self.embed(np.sin(np.arange(RATE) * 0.05).astype(np.float32) * 0.1)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        """A unit-length voiceprint of ``samples`` (16 kHz mono float32, at most ~MAX_EMBED_S)."""
        x = np.ascontiguousarray(samples, dtype=np.float32).reshape(-1)
        with self._lock:
            stream = self._ex.create_stream()
            stream.accept_waveform(RATE, x)
            stream.input_finished()
            v = np.asarray(self._ex.compute(stream), dtype=np.float32)
            del stream
        return unit(v)


def unit(values: Any) -> np.ndarray:
    """Finite numbers as a unit-length float32 vector (ValueError otherwise)."""
    v = np.asarray(values, dtype=np.float32).reshape(-1)
    if not v.size or not np.all(np.isfinite(v)):
        raise ValueError("a voiceprint is a list of finite numbers")
    norm = float(np.linalg.norm(v))
    if norm < 1e-6:
        raise ValueError("a voiceprint can't be all zeros")
    return v / norm


def to_model_rate(samples: np.ndarray, rate: int) -> np.ndarray:
    """Mono float32 at 16 kHz (int16 is scaled to -1..1)."""
    x = np.asarray(samples)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    x = dsp.as_mono_float(x) if x.ndim > 1 else np.asarray(x, dtype=np.float32).reshape(-1)
    return dsp.resample(x, int(rate), RATE) if int(rate) != RATE else x


def speech_only(x: np.ndarray, rate: int = RATE) -> np.ndarray:
    """The loud-enough 20 ms frames of ``x`` (pauses, silence and the call's muted frames left out)."""
    n = max(1, int(rate * FRAME_S))
    frames = len(x) // n
    if frames == 0:
        return np.zeros(0, dtype=np.float32)
    f = np.asarray(x[: frames * n], dtype=np.float32).reshape(frames, n)
    levels = np.sqrt(np.mean(f * f, axis=1))
    loud = float(np.percentile(levels, 95))
    keep = levels >= max(MIN_LEVEL, REL_LEVEL * loud)
    if HANGOVER_FRAMES and keep.any():
        held = keep.copy()
        for k in range(1, min(HANGOVER_FRAMES, frames - 1) + 1):
            held[k:] |= keep[:-k]  # after speech
            held[:-k] |= keep[k:]  # and just before it
        keep = held & (levels >= MIN_LEVEL / 2)
    return f[keep].reshape(-1).copy()


def windows(speech: np.ndarray, seconds: float, *, min_seconds: float, rate: int = RATE) -> list[np.ndarray]:
    """``speech`` cut into pieces of ``seconds`` (a last piece shorter than ``min_seconds`` joins the one before)."""
    n, m = int(seconds * rate), int(min_seconds * rate)
    out = [speech[i:i + n] for i in range(0, len(speech), n)]
    if len(out) > 1 and len(out[-1]) < m:
        last = out.pop()
        out[-1] = np.concatenate([out[-1], last])
    return [w for w in out if len(w) >= m]


__all__ = [
    "MAX_EMBED_S",
    "RATE",
    "EngineUnavailable",
    "SpeakerEngine",
    "sherpa_installed",
    "speech_only",
    "to_model_rate",
    "unit",
    "windows",
]
