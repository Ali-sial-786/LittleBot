"""What the speech-to-text providers share: the shape of a provider that only transcribes (it gets LittleBot's own
end-of-turn, stt/endpoint.py) and handing events to the session.

A batch provider (:class:`BatchSTT`) turns one finished turn into words (``transcribe``). One that can also follow
the words while they are said (Vosk) offers a ``decoder()`` for each turn (:class:`TurnDecoder`): it gets the
turn's audio as it comes, says the words so far (the endpointer waits longer after "and ..." than after a whole
sentence), and the last words are ready the moment the turn ends.
"""

from __future__ import annotations

import inspect
import logging
from typing import Protocol, runtime_checkable

import numpy as np

from littlebot.assistant.speech import OnEvent, STTEvent

log = logging.getLogger("littlebot.assistant.stt")


@runtime_checkable
class BatchSTT(Protocol):
    name: str

    def missing(self) -> str | None: ...

    async def transcribe(self, samples: np.ndarray) -> str:
        """The words in 16 kHz float32 mono ``samples`` ("" for none). Raises SpeechError."""


@runtime_checkable
class TurnDecoder(Protocol):
    async def accept(self, samples: np.ndarray) -> str:
        """More of the turn (16 kHz float32 mono) -> the words so far ("" for none yet). Raises SpeechError."""

    async def final(self) -> str:
        """The turn is over: all its words. Raises SpeechError."""

    async def aclose(self) -> None: ...


async def emit(on_event: OnEvent, event: STTEvent) -> None:
    """Give ``event`` to the session's callback (plain or async); a failing callback is logged, never raised."""
    try:
        result = on_event(event)
        if inspect.isawaitable(result):
            await result
    except Exception:  # noqa: BLE001 - the session's bug must not stop listening
        log.warning("assistant stt: the %s handler failed", event.kind, exc_info=True)


__all__ = ["BatchSTT", "TurnDecoder", "emit"]
