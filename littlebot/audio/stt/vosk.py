"""Vosk offline speech recognition (optional: ``pip install vosk`` + a small model).

Recommended model for the Pi 5 (2 GB): ``vosk-model-small-en-us-0.15`` (~40 MB,
~300 MB RAM while loaded). Configure ``audio.stt.vosk_model_path`` (directory).
UNVERIFIED on the Pi; accuracy on toddler speech is low - LittleBot only uses
it for short caregiver words.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from littlebot.audio import dsp
from littlebot.audio.errors import STTError
from littlebot.audio.stt.base import STTProvider, STTResult
from littlebot.core.paths import PROJECT_ROOT

log = logging.getLogger("littlebot.audio.stt.vosk")


def vosk_installed() -> bool:
    try:
        return importlib.util.find_spec("vosk") is not None
    except (ImportError, ValueError):
        return False


class VoskSTT(STTProvider):
    name = "vosk"

    def __init__(self, model_path: str | None, *, sample_rate: int = 16000) -> None:
        self.model_path = self._resolve(model_path)
        self.sample_rate = sample_rate
        self._model: Any = None
        self._lock = threading.Lock()

    @staticmethod
    def _resolve(model_path: str | None) -> Path | None:
        if not model_path:
            return None
        p = Path(model_path).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    def available(self) -> bool:
        return self.model_path is not None and self.model_path.is_dir() and vosk_installed()

    def unavailable_reason(self) -> str:
        if not vosk_installed():
            return "vosk is not installed (pip install vosk)"
        if self.model_path is None:
            return "audio.stt.vosk_model_path is not configured"
        if not self.model_path.is_dir():
            return f"vosk model directory not found: {self.model_path}"
        return ""

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                if not self.available():
                    raise STTError(self.unavailable_reason())
                import vosk  # noqa: PLC0415 - optional dependency

                vosk.SetLogLevel(-1)
                log.info("loading vosk model %s", self.model_path)
                self._model = vosk.Model(str(self.model_path))
            return self._model

    def transcribe(self, samples: np.ndarray, sample_rate: int) -> STTResult | None:
        model = self._load()
        import vosk  # noqa: PLC0415

        if sample_rate != self.sample_rate:
            samples = dsp.resample(samples, sample_rate, self.sample_rate)
        try:
            rec = vosk.KaldiRecognizer(model, float(self.sample_rate))
            rec.SetWords(True)
            rec.AcceptWaveform(dsp.to_int16(samples).tobytes())
            result = json.loads(rec.FinalResult() or "{}")
        except Exception as e:  # noqa: BLE001
            raise STTError(f"vosk failed: {e}") from e
        text = str(result.get("text", "")).strip()
        if not text:
            return None
        words = result.get("result") or []
        confs = [float(w.get("conf", 1.0)) for w in words if isinstance(w, dict)]
        return STTResult(text, round(sum(confs) / len(confs), 3) if confs else 0.8)
