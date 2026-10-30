"""AssistantService (``ctx.assistant``): the fast voice pipeline's front door.

* ``start_session(parent_present=, carry=)`` / ``stop_session(reason)`` / ``status()``: the same as GPT-Live's
  (``ctx.live``), so the wake word starts whichever is in use (:func:`littlebot.assistant.voice_front`) and the
  dashboard shows either. One session at a time (:class:`~littlebot.assistant.session.VoiceSession`).
* ``use_cascade()``: ``assistant.pipeline`` auto = this pipeline when a streaming speech-to-text key (Deepgram) and
  a fast voice key (Cartesia or ElevenLabs) are set, else GPT-Live as before; cascade / live force one.
* ``handle_text(text, speak=)``: the same brain for typed chat (the dashboard's Assistant page), optionally said
  aloud.
* ``speak(text)``: say something with the assistant's voice outside a session (an announcement).
* Announcements (bus ``assistant.announce``, littlebot/skills/base.py: a reminder rang, a game's tap was judged):
  said in the session if one is on, else passed to GPT-Live's call as news, else spoken through the speaker.
* ``history``: the last exchanges, in RAM only (``assistant.history_minutes`` / ``history_turns``), so "and
  tomorrow?" works across follow-ups and the next wake word.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import Any, Callable

import numpy as np

from littlebot.core.events import Event, Subscription, Topics
from littlebot.core.service import Service, ServiceStatus

log = logging.getLogger("littlebot.assistant")

ERRORS = {
    "already_running": "A conversation is already on.",
    "parent_not_present": "A grown-up needs to be there to start a conversation.",
    "offline": "LittleBot is in offline mode.",
    "cloud_off": "Cloud communication is switched off on the Privacy page.",
    "mic_off": "The microphone is switched off on the Privacy page.",
    "no_audio": "LittleBot's audio isn't running.",
    "busy": "LittleBot is busy with something more important.",
    "no_stt": "No speech-to-text is set up.",
}


class History:
    """The last exchanges (user / assistant pairs) in memory only, forgotten after ``minutes``."""

    def __init__(self, *, turns: int, minutes: float, now: Callable[[], float] = time.monotonic) -> None:
        self.turns = int(turns)
        self.minutes = float(minutes)
        self._now = now
        self._items: collections.deque[tuple[float, str, str]] = collections.deque(maxlen=max(1, self.turns))

    def add(self, user: str, assistant: str) -> None:
        if self.turns <= 0 or not (user.strip() or assistant.strip()):
            return
        self._items.append((self._now(), user.strip()[:2000], assistant.strip()[:2000]))

    def messages(self) -> list[dict[str, str]]:
        cutoff = self._now() - self.minutes * 60
        while self._items and self._items[0][0] < cutoff:
            self._items.popleft()
        out: list[dict[str, str]] = []
        for _, user, assistant in self._items:
            out += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
        return out

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class AssistantService(Service):
    name = "assistant"

    def __init__(self, ctx: Any, *, audio_factory: Callable[[], Any] | None = None,
                 stt_factory: Callable[..., Any] | None = None, tts_factory: Callable[[], Any] | None = None,
                 brain: Any = None, now: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.ctx = ctx
        self.cfg = ctx.config.assistant
        self.now = now
        self._audio_factory = audio_factory
        self._stt_factory = stt_factory
        self._tts_factory = tts_factory
        self._tts: Any = None
        if brain is None:
            from littlebot.assistant.agent import Brain  # noqa: PLC0415

            brain = Brain(ctx, now=now)
        self.brain = brain
        self.history = History(turns=self.cfg.history_turns, minutes=self.cfg.history_minutes, now=now)
        self._session: Any = None
        self._task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: list[Subscription] = []
        self.last_call: dict[str, Any] | None = None
        self._told_pipeline: str | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        bus = self.ctx.bus
        from littlebot.skills.base import ANNOUNCE  # noqa: PLC0415

        self._subs = [
            bus.subscribe(Topics.STOP_ALL, lambda e: self._end_threadsafe("stopped (STOP ALL)")),
            bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy),
            bus.subscribe(ANNOUNCE, self._on_announce),
            bus.subscribe("game.news", self._on_game_news),
        ]
        why = self.pipeline_reason()
        log.info("assistant: voice pipeline %s (%s)", "assistant" if self.use_cascade() else "GPT-Live", why)
        self.set_status(ServiceStatus.RUNNING, why)

    async def stop(self) -> None:
        for s in self._subs:
            s.cancel()
        self._subs.clear()
        await self.stop_session("service stop")
        with contextlib.suppress(Exception):
            await self.brain.aclose()
        if self._tts is not None:
            with contextlib.suppress(Exception):
                await self._tts.aclose()
        self.set_status(ServiceStatus.STOPPED)

    # ------------------------------------------------------------ which pipeline
    def keys(self) -> dict[str, bool]:
        c = self.ctx.config
        return {name: bool(c.secret(f"{name}_api_key")) for name in ("deepgram", "cartesia", "elevenlabs")} | {
            "openai": bool(c.openai_api_key and c.openai_api_key.get_secret_value()),
            "anthropic": bool(c.anthropic_api_key and c.anthropic_api_key.get_secret_value())}

    def use_cascade(self) -> bool:
        mode = self.cfg.pipeline
        if mode == "live":
            return False
        if mode == "cascade":
            return True
        k = self.keys()
        return k["deepgram"] and (k["cartesia"] or k["elevenlabs"])

    def pipeline_reason(self) -> str:
        mode = self.cfg.pipeline
        if mode != "auto":
            return f"assistant.pipeline is {mode}"
        k = self.keys()
        if self.use_cascade():
            return "auto: Deepgram and " + ("Cartesia" if k["cartesia"] else "ElevenLabs") + " keys are set"
        missing = [n for n, ok in (("DEEPGRAM_API_KEY", k["deepgram"]),
                                   ("CARTESIA_API_KEY or ELEVENLABS_API_KEY", k["cartesia"] or k["elevenlabs"]))
                   if not ok]
        return "auto: GPT-Live until " + " and ".join(missing) + " are set"

    # ------------------------------------------------------------ factories
    @property
    def audio_factory(self) -> Callable[[], Any]:
        return self._audio_factory or self._default_audio

    def _default_audio(self) -> Any:
        """The call audio GPT-Live uses (full duplex, echo gate, barge-in, lip-sync), with the assistant's switch."""
        from littlebot.chatgpt.live_audio import BargeInSettings, LiveAudio  # noqa: PLC0415

        a = self.ctx.config.audio
        live_cfg = self.ctx.config.chatgpt
        max_vol = float(a.speaker.max_volume)

        def volume() -> float:
            v = self.ctx.settings.get("audio.volume", a.speaker.volume)
            try:
                return max(0.0, min(max_vol, float(v)))
            except (TypeError, ValueError):
                return min(max_vol, float(a.speaker.volume))

        def gain() -> float:
            try:
                return float(self.ctx.settings.get("audio.input_gain", a.microphone.input_gain) or 1.0)
            except (TypeError, ValueError):
                return 1.0

        barge = BargeInSettings.from_config(live_cfg)
        if not self.cfg.barge_in:
            barge = BargeInSettings(mode="off")
        return LiveAudio(mic=a.microphone.device, speaker=a.speaker.device, volume=volume, mic_gain=gain,
                         half_duplex=live_cfg.live_half_duplex, echo_tail_s=live_cfg.live_echo_tail_ms / 1000,
                         voice_level=live_cfg.live_voice_level, barge_in=barge)

    def tts(self) -> Any:
        if self._tts is None:
            if self._tts_factory is not None:
                self._tts = self._tts_factory()
            else:
                from littlebot.assistant.tts import build_tts_chain  # noqa: PLC0415

                self._tts = build_tts_chain(self.ctx)
        return self._tts

    def stt_provider(self, exclude: list[str] | tuple[str, ...] = ()) -> Any:
        """The first speech-to-text that works, leaving out ``exclude`` (ones that failed in this session)."""
        if self._stt_factory is not None:
            return self._stt_factory(exclude=list(exclude))
        from littlebot.assistant.stt import build_stt  # noqa: PLC0415

        return build_stt(self.ctx, exclude=list(exclude))

    # ------------------------------------------------------------ sessions
    def blocked_reason(self, parent_present: bool = True) -> str | None:
        ctx = self.ctx
        if self._session is not None:
            return "already_running"
        if not parent_present:
            return "parent_not_present"
        if ctx.config.is_offline and not self._local_only():
            return "offline"
        if not ctx.privacy.cloud_enabled and not self._local_only():
            return "cloud_off"
        if not ctx.privacy.microphone_enabled:
            return "mic_off"
        return None

    def _local_only(self) -> bool:
        return False  # the brain needs the cloud (a local model doesn't fit the Pi yet)

    def status(self) -> dict[str, Any]:
        session = self._session
        reason = self.blocked_reason() if session is None else None
        out: dict[str, Any] = {
            "available": True,
            "pipeline": "assistant" if self.use_cascade() else "live",
            "pipeline_setting": self.cfg.pipeline,
            "pipeline_reason": self.pipeline_reason(),
            "keys": self.keys(),  # whether each key is set (never the keys)
            "can_start": session is None and reason is None,
            "blocked": reason,
            "blocked_detail": ERRORS.get(reason or ""),
            "active": session is not None,
            "history_turns": len(self.history),
            "last_call": self.last_call,
        }
        if self._tts is not None:
            with contextlib.suppress(Exception):
                out["tts"] = self._tts.describe()
        if session is not None:
            out.update(session.snapshot())
        return out

    async def start_session(self, *, parent_present: bool = True, carry: Any = None,
                            source: str = "wake") -> dict[str, Any]:
        """As GPT-Live's: ``carry`` is what was said with the wake word (dropped by the caller if this fails)."""
        reason = self.blocked_reason(parent_present)
        if reason:
            return {"ok": False, "error": reason, "detail": ERRORS[reason]}
        if self.ctx.audio is None:
            return {"ok": False, "error": "no_audio", "detail": ERRORS["no_audio"]}
        from littlebot.assistant.session import VoiceSession, lease_for  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        session = VoiceSession(self, carry=carry, source=source)
        lease = lease_for(self, lambda why: self._end_threadsafe(why))
        if lease is None:
            return {"ok": False, "error": "busy", "detail": ERRORS["busy"]}
        session.lease = lease
        self._session = session
        self._task = self._loop.create_task(self._run(session), name="assistant-session")
        return {"ok": True, **self.status()}

    async def _run(self, session: Any) -> None:
        try:
            await session.run()
        finally:
            if self._session is session:
                self._session = None
            self.last_call = session.summary()
        done = await session.run_handover()
        if done and self.last_call is not None:
            self.last_call["handed_over"] = done

    async def stop_session(self, reason: str = "stopped") -> dict[str, Any]:
        session, task = self._session, self._task
        if session is None or task is None:
            return {"ok": True, **self.status()}
        session.end(reason)
        try:
            await asyncio.wait_for(asyncio.shield(task), 8.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        return {"ok": True, **self.status()}

    def _end_threadsafe(self, reason: str) -> None:
        session, loop = self._session, self._loop
        if session is None or loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(session.end, reason)

    def _on_privacy(self, event: Event) -> None:
        p = self.ctx.privacy
        if not (p.cloud_enabled and p.microphone_enabled):
            self._end_threadsafe("stopped (privacy settings changed)")

    # ------------------------------------------------------------ typed chat
    async def handle_text(self, text: str, *, speak: bool = False, source: str = "dashboard") -> dict[str, Any]:
        """The brain for typed words (the dashboard): ``{reply, tools, error, provider, model, ms}``. ``speak``: also
        said aloud (when no voice session is on)."""
        text = " ".join(str(text or "").split())[:2000]
        if not text:
            return {"reply": "", "tools": [], "error": "empty"}
        if self.ctx.config.is_offline:
            return {"reply": "", "tools": [], "error": "offline"}
        if not self.ctx.privacy.cloud_enabled:
            return {"reply": "", "tools": [], "error": "cloud_off"}
        from littlebot.assistant import intents  # noqa: PLC0415
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        box = RobotToolbox(self.ctx, mode="chat", defer=False)
        t0 = self.now()
        hit = intents.match(text)
        if hit is not None and hit.name in ("volume", "time"):
            reply = await intents.run(hit, box, self.ctx)
            out = {"reply": reply, "tools": list(box.calls), "error": None, "provider": "intent", "model": "",
                   "ms": int((self.now() - t0) * 1000)}
        else:
            said: list[str] = []
            res = await self.brain.respond(text, history=self.history.messages(), toolbox=box,
                                           on_sentence=lambda s: said.append(s.text))
            reply = " ".join(said)
            if res.error and not reply:
                from littlebot.assistant.session import SORRY, SORRY_DEFAULT  # noqa: PLC0415

                reply = SORRY.get(res.error, SORRY_DEFAULT)
            out = {"reply": reply, "tools": list(res.tools), "error": res.error, "provider": res.provider,
                   "model": res.model, "ms": int((self.now() - t0) * 1000), "silent": res.silent}
        if reply:
            self.history.add(text, reply)
        if speak and reply and self._session is None:
            asyncio.ensure_future(self.speak(reply))
        return out

    # ------------------------------------------------------------ speaking outside a session
    async def speak(self, text: str) -> bool:
        """Say ``text`` with the assistant's voice through the speaker (no session needed). Falls back to the
        robot's own text-to-speech."""
        audio = self.ctx.audio
        if audio is None or not text.strip():
            return False
        play = getattr(audio, "play_speech", None)
        if play is not None and self.ctx.privacy.cloud_enabled and not self.ctx.config.is_offline:
            try:
                chunks = [np.asarray(c, dtype=np.float32) async for c in self.tts().stream(text)]
                if chunks:
                    return bool(await play(np.concatenate(chunks), int(getattr(self.tts(), "sample_rate", 48000)),
                                           text))
            except Exception as e:  # noqa: BLE001 - the plain voice instead
                log.info("assistant: speaking with the robot's own voice (%s)", getattr(e, "kind", type(e).__name__))
        return bool(await audio.say(text))

    def _on_announce(self, event: Event) -> None:
        text = str(event.get("text") or "").strip()
        card = event.get("card")
        loop = self._loop
        if not text or loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._announce(text, card)))

    def _on_game_news(self, event: Event) -> None:
        """An on-screen game's news ("you picked rock, I picked paper"): said in the session (GPT-Live says it in its
        own call)."""
        session = self._session
        text = str(event.get("text") or "").strip()
        if session is not None and text and self._loop is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(session.announce, text)

    async def _announce(self, text: str, card: Any = None) -> None:
        if isinstance(card, dict) and getattr(self.ctx, "screens", None) is not None:
            with contextlib.suppress(Exception):
                await self.ctx.screens.show(card, source="assistant")
        session = self._session
        if session is not None and not session.ended:
            session.announce(text)
            return
        live = getattr(self.ctx, "live", None)
        if live is not None and _active(live) and hasattr(live, "_to_call"):
            live._to_call("game_news", text)  # GPT-Live says it straight away  # noqa: SLF001
            return
        await self.speak(text)


def _active(svc: Any) -> bool:
    try:
        return bool(svc.status().get("active"))
    except Exception:  # noqa: BLE001
        return False


__all__ = ["AssistantService", "ERRORS", "History"]
