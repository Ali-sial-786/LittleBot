"""The assistant's speech services: what every text-to-speech and speech-to-text provider looks like.

Text-to-speech (littlebot/assistant/tts): ``await tts.stream(text)`` yields float32 mono chunks (-1..1) at
``tts.sample_rate`` as they arrive from the service, so the first words play while the rest is still being made.
``TTSChain`` (tts/__init__.py) tries the configured providers in order (cartesia, elevenlabs, openai, local): one
that isn't set up is skipped, one that fails or says nothing within ``tts_first_audio_timeout_seconds`` hands the
same sentence to the next, as long as nothing of it was played yet.

Speech-to-text (littlebot/assistant/stt): a provider opens a ``STTStream`` for one session; the session feeds it
16 kHz float32 mono microphone audio (``feed``) and gets :class:`STTEvent` s back through the ``on_event`` callback
given to ``open``: words so far (``partial``), the turn's words when the person has finished their turn
(``end_of_turn``: the cue to answer), and ``start_of_turn`` when they start talking (barge-in confirmation).
Providers without their own end-of-turn detection get LittleBot's (stt/endpoint.py) around them.

Rules for every provider (docs/DEV_CONVENTIONS.md): keys only from ``ctx.config`` (SecretStr; never logged,
never in an error message), network only while ``cloud_ok()`` says so, nothing recorded or stored, no blocking
of the event loop (blocking work in a thread), errors as :class:`SpeechError` with a short ``kind``.
"""

from __future__ import annotations

import dataclasses
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, runtime_checkable

import numpy as np

STT_RATE = 16000  # what the session feeds every STT provider


class SpeechError(Exception):
    """A speech service failed. ``kind``: no_key | not_installed | cloud_off | auth | rate_limit | timeout |
    network | server_error | bad_audio | closed."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@runtime_checkable
class StreamingTTS(Protocol):
    name: str
    sample_rate: int  # of the chunks ``stream`` yields

    def missing(self) -> str | None:
        """Why it can't be used now ("no_key", "not_installed", "cloud_off"), else None."""

    def stream(self, text: str) -> AsyncIterator[np.ndarray]:
        """The sentence as float32 mono chunks, first chunk as soon as the service sends audio. Raises
        SpeechError (before or between chunks)."""

    async def warm(self) -> None:
        """Open the connection before the first sentence (best effort, never raises)."""

    async def aclose(self) -> None: ...


@dataclasses.dataclass(frozen=True)
class STTEvent:
    kind: str  # "start_of_turn" | "partial" | "end_of_turn" | "error"
    text: str = ""
    confidence: float = 1.0
    detail: str = ""  # error: SpeechError.kind


OnEvent = Callable[[STTEvent], Any]


@runtime_checkable
class STTStream(Protocol):
    async def feed(self, samples: np.ndarray) -> None:
        """16 kHz float32 mono audio, any length (the session sends ~20-100 ms at a time)."""

    async def finish(self) -> None:
        """No more audio for this turn is coming (the session hit max_turn_seconds): end the turn now."""

    async def reset(self) -> None:
        """Forget the turn so far (LittleBot answered; the next words are a new turn)."""

    async def aclose(self) -> None: ...


@runtime_checkable
class STTProvider(Protocol):
    name: str
    streaming: bool  # True: it finds the end of a turn itself (Deepgram Flux)

    def missing(self) -> str | None: ...

    async def open(self, on_event: OnEvent) -> STTStream:
        """A stream for one session. Raises SpeechError."""


CloudOk = Callable[[], bool]
Transcribe = Callable[[np.ndarray], Awaitable[str]]

__all__ = ["CloudOk", "OnEvent", "STTEvent", "STTProvider", "STTStream", "STT_RATE", "SpeechError", "StreamingTTS",
           "Transcribe"]
