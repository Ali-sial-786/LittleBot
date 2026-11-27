"""Vosk on the robot (offline, no key): the last speech-to-text, with LittleBot's own end-of-turn around it.

It uses the model the wake word already has loaded (littlebot/audio/stt/vosk.py ``load_model`` keeps one copy for
everyone): ``audio.stt.vosk_model_path`` when that is set and there, else ``wake.model_path`` (the small English
model, ``scripts/download_models.py vosk-en``). The whole vocabulary (no grammar), at 16 kHz. Each turn gets its
own recogniser (:class:`VoskDecoder`) that follows the words while they are said, in a worker thread, so the
endpointer knows the words so far and the last ones are ready as soon as the turn ends. Accuracy is well below the
cloud services (the small model is made for commands), but it works with no network at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np

from littlebot.assistant.speech import STT_RATE, SpeechError
from littlebot.audio import dsp
from littlebot.audio.stt.vosk import load_model, model_present, resolve_model_path, vosk_installed

log = logging.getLogger("littlebot.assistant.stt.vosk")


def model_path_for(config: Any) -> Path | None:
    """The first of ``audio.stt.vosk_model_path`` and ``wake.model_path`` that holds a Vosk model (else the first
    one set, so ``missing()`` can say "no_model")."""
    paths = [resolve_model_path(getattr(config.audio.stt, "vosk_model_path", None)),
             resolve_model_path(getattr(config.wake, "model_path", None))]
    paths = [p for p in paths if p is not None]
    return next((p for p in paths if model_present(p)), paths[0] if paths else None)


def _recognizer(model: Any, rate: int) -> Any:
    import vosk  # noqa: PLC0415 - optional dependency

    return vosk.KaldiRecognizer(model, float(rate))


class VoskDecoder:
    """One turn's recogniser (a ``TurnDecoder``): made in the worker thread on the first audio."""

    def __init__(self, model: Any, *, recognizer: Callable[[Any, int], Any] = _recognizer,
                 sample_rate: int = STT_RATE) -> None:
        self._model = model
        self._make = recognizer
        self.sample_rate = sample_rate
        self._rec: Any = None
        self._done: list[str] = []  # sentences Vosk already closed within this turn
        self._lock = asyncio.Lock()  # one thread call at a time, in order

    def _accept(self, pcm: bytes) -> str:
        if self._rec is None:
            self._rec = self._make(self._model, self.sample_rate)
        if self._rec.AcceptWaveform(pcm):
            text = str(json.loads(self._rec.Result() or "{}").get("text", "")).strip()
            if text:
                self._done.append(text)
            return " ".join(self._done)
        partial = str(json.loads(self._rec.PartialResult() or "{}").get("partial", "")).strip()
        return " ".join([*self._done, partial] if partial else self._done)

    def _final(self) -> str:
        if self._rec is not None:
            text = str(json.loads(self._rec.FinalResult() or "{}").get("text", "")).strip()
            if text:
                self._done.append(text)
        return " ".join(self._done)

    async def accept(self, samples: np.ndarray) -> str:
        pcm = dsp.to_int16(samples).tobytes()
        async with self._lock:
            try:
                return await asyncio.to_thread(self._accept, pcm)
            except Exception as e:  # noqa: BLE001 - vosk raises plain exceptions
                raise SpeechError("bad_audio", type(e).__name__) from None

    async def final(self) -> str:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._final)
            except Exception as e:  # noqa: BLE001
                raise SpeechError("bad_audio", type(e).__name__) from None

    async def aclose(self) -> None:
        self._rec, self._done = None, []


class VoskSTT:
    """A ``BatchSTT`` with a per-turn ``decoder()`` (used through stt/endpoint.py)."""

    name = "vosk"
    streaming = False
    is_cloud = False

    def __init__(self, model_path: Path | None, *, load: Callable[[Path], Any] = load_model,
                 recognizer: Callable[[Any, int], Any] = _recognizer, installed: Callable[[], bool] = vosk_installed,
                 sample_rate: int = STT_RATE) -> None:
        self.model_path = model_path
        self._load = load
        self._recognizer = recognizer
        self._installed = installed
        self.sample_rate = sample_rate
        self._model: Any = None  # held while this provider lives (the wake word shares it)

    def missing(self) -> str | None:
        if not self._installed():
            return "not_installed"
        if not model_present(self.model_path):
            return "no_model"
        return None

    async def warm(self) -> None:
        """Load the model (in a thread: ~1 s the first time, nothing when the wake word has it). Raises SpeechError."""
        if self._model is not None:
            return
        why = self.missing()
        if why:
            raise SpeechError(why)
        try:
            self._model = await asyncio.to_thread(self._load, self.model_path)
        except Exception as e:  # noqa: BLE001 - ImportError, or vosk's plain Exception for an unreadable model
            log.warning("assistant stt: the vosk model could not be loaded (%s)", type(e).__name__)
            raise SpeechError("no_model", type(e).__name__) from None

    def decoder(self) -> VoskDecoder:
        if self._model is None:
            raise SpeechError("no_model", "not loaded")
        return VoskDecoder(self._model, recognizer=self._recognizer, sample_rate=self.sample_rate)

    async def transcribe(self, samples: np.ndarray) -> str:
        await self.warm()
        decoder = self.decoder()
        try:
            await decoder.accept(samples)
            return await decoder.final()
        finally:
            await decoder.aclose()

    async def aclose(self) -> None:
        self._model = None


__all__ = ["VoskDecoder", "VoskSTT", "model_path_for"]
