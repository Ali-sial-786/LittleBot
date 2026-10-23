"""Vosk offline speech recognition (optional: ``pip install vosk`` + a small model).

Recommended model for the Pi 5 (2 GB): ``vosk-model-small-en-us-0.15`` (~40 MB download,
~130 MB RAM while loaded; ``scripts/download_models.py vosk-en`` puts it in
``models/vosk-model-small-en-us-0.15``). Configure ``audio.stt.vosk_model_path`` (directory).
UNVERIFIED on the Pi; accuracy on toddler speech is low - LittleBot only uses
it for short caregiver words. The wake word (``littlebot.audio.wakeword``) uses the same
model: ``load_model`` keeps one copy in memory for everyone who loads the same directory.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import threading
import weakref
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


def resolve_model_path(model_path: str | Path | None) -> Path | None:
    """A model directory from config: ``~`` expanded, relative paths from the project root."""
    if not model_path:
        return None
    p = Path(model_path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def model_present(path: Path | None) -> bool:
    """``path`` looks like an unpacked Vosk model (``conf/model.conf`` or ``am/final.mdl``)."""
    return path is not None and path.is_dir() and ((path / "conf" / "model.conf").is_file()
                                                   or (path / "am" / "final.mdl").is_file())


# One vosk.Model per directory, shared (STT + wake word) and freed when nobody holds it any more.
_MODELS: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()
_MODELS_LOCK = threading.Lock()


def load_model(path: Path) -> Any:
    """The ``vosk.Model`` for ``path`` (loaded once, ~1 s; shared while anyone holds a reference).

    Raises ImportError when vosk is missing; vosk raises Exception for an unreadable model."""
    key = str(Path(path).resolve())
    with _MODELS_LOCK:
        model = _MODELS.get(key)
        if model is None:
            import vosk  # noqa: PLC0415 - optional dependency

            vosk.SetLogLevel(-1)
            log.info("loading vosk model %s", key)
            model = vosk.Model(key)
            _MODELS[key] = model
        return model


class VoskSTT(STTProvider):
    name = "vosk"

    def __init__(self, model_path: str | None, *, sample_rate: int = 16000) -> None:
        self.model_path = self._resolve(model_path)
        self.sample_rate = sample_rate
        self._model: Any = None
        self._lock = threading.Lock()

    @staticmethod
    def _resolve(model_path: str | None) -> Path | None:
        return resolve_model_path(model_path)

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
                assert self.model_path is not None
                self._model = load_model(self.model_path)
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
