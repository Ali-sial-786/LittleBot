"""eSpeak NG voice (offline, tiny, robotic but clear). ``sudo apt install espeak-ng``."""

from __future__ import annotations

import logging
import shutil
import subprocess

import numpy as np

from littlebot.audio import dsp
from littlebot.audio.errors import TTSError
from littlebot.audio.tts.base import TTSProvider

log = logging.getLogger("littlebot.audio.tts.espeak")


class EspeakTTS(TTSProvider):
    name = "espeak"

    def __init__(self, *, voice: str = "en-us+f3", pitch: int = 65, base_wpm: int = 160, amplitude: int = 100,
                 word_gap: int = 2, binary: str | None = None, timeout: float = 15.0) -> None:
        self.voice = voice or "en-us"
        self.pitch = max(0, min(99, int(pitch)))
        self.base_wpm = base_wpm
        self.amplitude = max(10, min(200, int(amplitude)))
        self.word_gap = max(0, int(word_gap))  # units of 10 ms: small pauses help little listeners
        self.binary = binary or shutil.which("espeak-ng") or shutil.which("espeak")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.binary)

    def unavailable_reason(self) -> str:
        return "" if self.binary else "espeak-ng is not installed (sudo apt install espeak-ng)"

    def _command(self, voice: str, rate: float) -> list[str]:
        wpm = int(max(80, min(260, self.base_wpm * rate)))
        return [str(self.binary), "--stdout", "--stdin", "-v", voice, "-p", str(self.pitch), "-s", str(wpm),
                "-a", str(self.amplitude), "-g", str(self.word_gap)]

    def synthesize(self, text: str, rate: float = 1.0) -> tuple[np.ndarray, int]:
        if not self.binary:
            raise TTSError(self.unavailable_reason())
        voices = [self.voice] + ([self.voice.split("+")[0]] if "+" in self.voice else [])
        last_err = ""
        for voice in voices:
            try:
                # Text goes through stdin, never the command line (no option injection).
                proc = subprocess.run(self._command(voice, rate), input=text.encode("utf-8"),
                                      capture_output=True, timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as e:
                raise TTSError("espeak-ng timed out") from e
            except OSError as e:
                raise TTSError(f"espeak-ng could not run: {e}") from e
            if proc.returncode == 0 and len(proc.stdout) > 44:
                try:
                    samples, sr = dsp.decode_wav(proc.stdout)
                except ValueError as e:
                    raise TTSError(f"espeak-ng produced unreadable audio: {e}") from e
                return dsp.prepare_speech(samples, sr), sr
            last_err = proc.stderr.decode("utf-8", "replace").strip()[:200] or f"exit code {proc.returncode}"
            log.debug("espeak-ng voice %s failed: %s", voice, last_err)
        raise TTSError(f"espeak-ng failed: {last_err}")
