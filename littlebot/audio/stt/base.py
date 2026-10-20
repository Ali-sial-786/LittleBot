"""Speech-to-text provider interface.

``transcribe`` is blocking and always runs in a worker thread. Audio is
passed in memory (mono float32, normally 16 kHz) and never written to disk.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any

import numpy as np


@dataclasses.dataclass(frozen=True)
class STTResult:
    text: str
    confidence: float = 1.0


class STTProvider(abc.ABC):
    name: str = "base"
    is_cloud: bool = False

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""

    @abc.abstractmethod
    def transcribe(self, samples: np.ndarray, sample_rate: int) -> STTResult | None:
        """Text heard, or None when nothing intelligible. Raises STTError / CloudNotAllowed."""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available(), "cloud": self.is_cloud,
                "detail": "" if self.available() else self.unavailable_reason()}
