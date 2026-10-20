"""Piper neural voice (offline, natural sounding) via the ``piper`` command-line tool.

Needs the ``piper`` binary (https://github.com/rhasspy/piper releases, or
``pip install piper-tts``) and a voice model (``*.onnx`` + ``*.onnx.json``),
e.g. ``en_US-amy-low``. Each phrase starts a process that loads the model:
on a Pi 5 a *low*/*medium* voice adds roughly 0.5-1.5 s per phrase (UNVERIFIED).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np

from littlebot.audio import dsp
from littlebot.audio.errors import TTSError
from littlebot.audio.tts.base import TTSProvider
from littlebot.core.paths import PROJECT_ROOT

log = logging.getLogger("littlebot.audio.tts.piper")


class PiperTTS(TTSProvider):
    name = "piper"

    def __init__(self, model_path: str | None, *, binary: str | None = None, timeout: float = 30.0) -> None:
        self.model_path = self._resolve(model_path)
        self.binary = binary or shutil.which("piper")
        self.timeout = timeout
        self._sample_rate: int | None = None

    @staticmethod
    def _resolve(model_path: str | None) -> Path | None:
        if not model_path:
            return None
        p = Path(model_path).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    def available(self) -> bool:
        return bool(self.binary) and self.model_path is not None and self.model_path.is_file()

    def unavailable_reason(self) -> str:
        if not self.binary:
            return "piper binary not found on PATH"
        if self.model_path is None:
            return "audio.tts.piper_model is not configured"
        if not self.model_path.is_file():
            return f"piper model not found: {self.model_path}"
        return ""

    def sample_rate(self) -> int:
        if self._sample_rate is None:
            rate = 22050
            assert self.model_path is not None
            cfg = Path(str(self.model_path) + ".json")
            try:
                rate = int(json.loads(cfg.read_text(encoding="utf-8"))["audio"]["sample_rate"])
            except (OSError, ValueError, KeyError, TypeError):
                log.debug("could not read piper model config %s; assuming %d Hz", cfg, rate)
            self._sample_rate = rate
        return self._sample_rate

    def synthesize(self, text: str, rate: float = 1.0) -> tuple[np.ndarray, int]:
        if not self.available():
            raise TTSError(self.unavailable_reason())
        length_scale = 1.0 / max(0.5, rate)
        cmd = [str(self.binary), "--model", str(self.model_path), "--output_raw", "--length_scale", f"{length_scale:.2f}"]
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            raise TTSError("piper timed out") from e
        except OSError as e:
            raise TTSError(f"piper could not run: {e}") from e
        if proc.returncode != 0 or len(proc.stdout) < 2:
            err = proc.stderr.decode("utf-8", "replace").strip()[-200:]
            raise TTSError(f"piper failed: {err or proc.returncode}")
        pcm = np.frombuffer(proc.stdout[: len(proc.stdout) // 2 * 2], dtype="<i2")
        sr = self.sample_rate()
        return dsp.prepare_speech(dsp.as_mono_float(pcm), sr), sr
