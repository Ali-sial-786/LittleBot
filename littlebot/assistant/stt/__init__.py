"""The assistant's ears: the configured speech-to-text services in order, as one ``STTProvider``.

``build_stt(ctx)`` makes an :class:`STTChain` from ``assistant.stt`` (default deepgram, openai, vosk), or None when
none of them can be used now (no key, cloud off and no Vosk model). ``await chain.open(on_event)`` gives the
stream of the first provider that opens: one that isn't set up or not allowed now (cloud off) is skipped (one log
line when that changes); one that fails to open, or whose stream reports an ``error`` event, rests for a while
(a refused key: 10 minutes; a lost connection: 20 s), so the session's next ``open`` gets the next provider.

Every stream it gives emits ``end_of_turn`` itself: Deepgram Flux finds the end of a turn on its own (``streaming``);
OpenAI and Vosk only transcribe, so they get LittleBot's endpointer around them (stt/endpoint.py).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from littlebot.assistant.speech import CloudOk, OnEvent, SpeechError, STTEvent, STTProvider, STTStream
from littlebot.assistant.speech_http import cloud_ok_for, is_allowed, key_reader
from littlebot.assistant.stt.deepgram import DeepgramFlux, WsConnect
from littlebot.assistant.stt.endpoint import EndpointedSTT, Endpointer, pause_needed_ms
from littlebot.assistant.stt.openai import OpenAITranscriber
from littlebot.assistant.stt.vosk import VoskSTT, model_path_for

log = logging.getLogger("littlebot.assistant.stt")

# after a failure, a cloud provider is skipped this long (seconds, by SpeechError kind)
REST_S = {"auth": 600.0, "bad_request": 600.0, "rate_limit": 60.0, "cloud_off": 0.0}
REST_DEFAULT_S = 20.0


class STTChain:
    """The providers in order as one ``STTProvider``. ``name`` / ``streaming``: of the one that would open now."""

    def __init__(self, providers: list[STTProvider], *, cloud_ok: CloudOk,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.providers = list(providers)
        self._cloud_ok = cloud_ok
        self._clock = clock
        self.last_provider: str | None = None
        self.last_error: str | None = None
        self._resting: dict[str, tuple[str, float]] = {}
        self._said_why: dict[str, str] = {}

    def _why_not(self, p: STTProvider) -> str | None:
        try:
            why = p.missing()
        except Exception:  # noqa: BLE001
            why = "error"
        if why:
            return why
        if getattr(p, "is_cloud", True) and not is_allowed(self._cloud_ok):  # a second lock on the cloud door
            return "cloud_off"
        rest = self._resting.get(p.name)
        if rest is not None:
            if rest[1] > self._clock():
                return f"resting after {rest[0]}"
            del self._resting[p.name]
        return None

    def _usable(self) -> list[STTProvider]:
        out = []
        for p in self.providers:
            why = self._why_not(p)
            if why is None:
                if self._said_why.pop(p.name, None) is not None:
                    log.info("assistant stt: %s can be used again", p.name)
                out.append(p)
            elif self._said_why.get(p.name) != why:
                self._said_why[p.name] = why
                log.info("assistant stt: %s skipped (%s)", p.name, why)
        return out

    def _first(self) -> STTProvider | None:
        return next((p for p in self.providers if self._why_not(p) is None), None)

    @property
    def name(self) -> str:
        p = self._first()
        return p.name if p is not None else (self.last_provider or "none")

    @property
    def streaming(self) -> bool:
        p = self._first()
        return bool(p.streaming) if p is not None else False

    def missing(self) -> str | None:
        whys = [self._why_not(p) for p in self.providers]
        return None if any(w is None for w in whys) else (whys[0] if whys else "not_installed")

    def _failed(self, p: STTProvider, kind: str) -> None:
        self.last_error = f"{p.name}: {kind}"
        if getattr(p, "is_cloud", True):
            rest = REST_S.get(kind, REST_DEFAULT_S)
            if rest > 0:
                self._resting[p.name] = (kind, self._clock() + rest)

    async def open(self, on_event: OnEvent) -> STTStream:
        """The first provider's stream that opens. Raises SpeechError when none does."""
        failure: SpeechError | None = None
        for p in self._usable():
            def watch(event: STTEvent, p: STTProvider = p) -> Any:
                if event.kind == "error":
                    self._failed(p, event.detail or "server_error")
                return on_event(event)

            try:
                stream = await p.open(watch)
            except SpeechError as e:
                log.warning("assistant stt: %s could not start (%s)", p.name, e.kind)
                self._failed(p, e.kind)
                failure = e
                continue
            self.last_provider = p.name
            return stream
        raise failure or SpeechError(self.missing() or "not_installed")

    async def aclose(self) -> None:
        for p in self.providers:
            close = getattr(p, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    log.debug("assistant stt: closing %s failed", p.name, exc_info=True)

    def describe(self) -> dict[str, Any]:
        """For the dashboard and the log: the order, who would listen now, who listened last (never a key)."""
        rows = [{"name": p.name, "streaming": bool(p.streaming), "cloud": bool(getattr(p, "is_cloud", True)),
                 "missing": self._why_not(p)} for p in self.providers]
        return {"order": [r["name"] for r in rows],
                "first": next((r["name"] for r in rows if r["missing"] is None), None),
                "last": self.last_provider, "last_error": self.last_error, "providers": rows}


def build_stt(ctx: Any, *, transport: Any = None, ws_connect: WsConnect | None = None,
              cloud_ok: CloudOk | None = None) -> STTChain | None:
    """The providers of ``ctx.config.assistant.stt`` in order, or None when none can be used now.
    ``transport``: an httpx transport (tests); ``ws_connect``: the websocket connector (tests); ``cloud_ok``: a
    stricter cloud gate than the default (offline mode + the parent's privacy switch)."""
    a = ctx.config.assistant
    ok = cloud_ok or cloud_ok_for(ctx)
    turn = {"end_of_turn_ms": a.end_of_turn_ms, "max_turn_seconds": a.max_turn_seconds,
            "threshold": ctx.config.audio.microphone.vad_threshold}
    providers: list[STTProvider] = []
    for name in dict.fromkeys(a.stt):
        if name == "deepgram":
            providers.append(DeepgramFlux(key=key_reader(ctx, "deepgram_api_key"), cloud_ok=ok, model=a.deepgram_model,
                                          eot_threshold=a.deepgram_eot_threshold,
                                          eot_timeout_ms=a.deepgram_eot_timeout_ms, ws_connect=ws_connect))
        elif name == "openai":
            providers.append(EndpointedSTT(OpenAITranscriber(key=key_reader(ctx, "openai_api_key"), cloud_ok=ok,
                                                             model=a.openai_stt_model, language=a.language,
                                                             transport=transport), **turn))
        elif name == "vosk":
            providers.append(EndpointedSTT(VoskSTT(model_path_for(ctx.config)), **turn))
    chain = STTChain(providers, cloud_ok=ok)
    rows = chain.describe()["providers"]
    log.info("assistant stt: %s", ", ".join(r["name"] + (f" ({r['missing']})" if r["missing"] else "") for r in rows)
             or "none configured")
    return chain if chain.missing() is None else None


__all__ = ["EndpointedSTT", "Endpointer", "STTChain", "build_stt", "pause_needed_ms"]
