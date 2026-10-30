"""One voice session: from the wake word until LittleBot stops listening for follow-ups.

::

    wake word ─▶ open the call audio (LiveAudio: full duplex, echo gate, barge-in, lip-sync)
             ─▶ speech-to-text stream (what was said with the wake word first, then the microphone)
    end of their turn ─▶ instant intent? ─▶ a voice game's move? ─▶ the brain (one streamed call)
             ─▶ each sentence to text-to-speech as soon as it is written ─▶ the speaker
    answer heard ─▶ listen for a follow-up (``assistant.follow_up_seconds``, no wake word) ─▶ … ─▶ quiet: end

Talking over LittleBot (barge-in, with echo cancelling as for GPT-Live): the call audio holds the voice the moment
a grown-up's voice is heard over it; their first words from speech-to-text confirm it (``barge_in_heard``), and
the answer being said is dropped and the new turn answered. "Stop" (the call audio's stop words) quiets it at once.
Words said while LittleBot is still thinking (before it speaks) are added to the question instead.

Take-overs (a video, an activity, sleep) asked for in a turn start once the answer was said and the session has
handed the speaker, microphone and screen back (``AssistantService`` runs ``run_pending``).

Nothing is recorded: audio passes through in-memory buffers only; the words are kept in RAM for the follow-ups
(``AssistantService.history``) and never written anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

import numpy as np

from littlebot.assistant import events as ev
from littlebot.assistant import intents
from littlebot.assistant.sentences import Sentence
from littlebot.assistant.speech import STT_RATE, STTEvent
from littlebot.assistant.voice_out import VoiceOut
from littlebot.core.arbiter import Priority, Resource
from littlebot.core.events import Topics
from littlebot.core.state import RobotState

log = logging.getLogger("littlebot.assistant")

OWNER = "assistant"
POLL_S = 0.05
THINKING_AFTER_S = 0.9  # a tool still running after this long: the soft thinking sound (again every REPEAT)
THINKING_REPEAT_S = 2.2
QUESTION_FOLLOW_UP_S = 12.0  # LittleBot asked them something: wait at least this long for the answer
HEARING_GRACE_S = 2.0  # a turn that never ends (noise): finished after max_turn_seconds + this
EXPRESSION_HOLD_S = 6.0
SORRY = {
    "no_key": "My brain isn't set up yet.",
    "auth_error": "My brain's key was refused. Check it on the dashboard.",
    "offline": "I'm offline right now.",
    "cloud_off": "My internet brain is switched off in the privacy settings.",
}
SORRY_DEFAULT = "Sorry, I couldn't think of an answer just then. Try me again?"


class SessionFailed(Exception):
    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind


class VoiceSession:
    def __init__(self, svc: Any, *, carry: Any = None, source: str = "wake") -> None:
        self.svc = svc
        self.ctx = svc.ctx
        self.cfg = svc.ctx.config.assistant
        self.carry = carry
        self.source = source
        self.lease: Any = None
        self.audio: Any = None
        self.stt: Any = None
        self.stt_name = ""
        self.phase = "connecting"
        self.detail = "Getting ready..."
        self.error: str | None = None
        self.end_reason: str | None = None
        self.heard_user = False
        self.connect_ms: int | None = None
        self.turns = 0
        self.barge_ins = 0
        self._now = svc.now
        self._started = self._now()
        self._ended = asyncio.Event()
        self._events: asyncio.Queue[STTEvent] = asyncio.Queue()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._answer: asyncio.Task[Any] | None = None
        self._answer_text = ""
        self._voice: VoiceOut | None = None
        self._toolbox: Any = None
        self._hearing_since: float | None = None
        self._deadline = self._started + self.cfg.first_words_seconds
        self._end_after: str | None = None  # end once the current answer was heard (said "bye")
        self._handover: list[Any] = []  # take-overs to run after the session (a video, an activity ...)
        self._suspended = False
        self._our_state: RobotState | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._failed_stt: list[str] = []

    # ------------------------------------------------------------ status
    def snapshot(self) -> dict[str, Any]:
        return {"phase": self.phase, "detail": self.detail, "heard_user": self.heard_user, "turns": self.turns,
                "connect_ms": self.connect_ms, "stt": self.stt_name,
                "seconds": round(self._now() - self._started, 1),
                "barge_in": bool(getattr(self.audio, "barge_in_on", False))}

    def summary(self) -> dict[str, Any]:
        return {"connected": self.connect_ms is not None, "error": self.error, "ended_reason": self.end_reason,
                "heard_user": self.heard_user, "turns": self.turns, "barge_ins": self.barge_ins,
                "seconds": round(self._now() - self._started, 1), "stt": self.stt_name}

    def end(self, reason: str) -> None:
        if not self._ended.is_set():
            self.end_reason = self.end_reason or reason
            self._ended.set()

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    # ------------------------------------------------------------ run
    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self._open()
            if not self._ended.is_set():
                await self._main()
        except asyncio.CancelledError:
            self.end_reason = self.end_reason or "cancelled"
            raise
        except SessionFailed as e:
            self.error = e.kind
            log.warning("assistant: the session could not start (%s%s)", e.kind, f": {e}" if str(e) != e.kind else "")
        except Exception as e:  # noqa: BLE001 - always clean up and report
            name = type(e).__name__
            self.error = "audio" if name == "AudioDeviceError" else name
            log.exception("assistant: the session failed")
        finally:
            await self._cleanup()

    async def _open(self) -> None:
        t0 = self._now()
        self._spawn(self._warm())  # the brain's and the voice's connections open while the microphone does
        audio_svc = self.ctx.audio
        if hasattr(audio_svc, "suspend"):
            await audio_svc.suspend("assistant")
            self._suspended = True
        self._set_phase("listening")
        self.audio = self.svc.audio_factory()
        await asyncio.to_thread(self.audio.open, self._loop)
        self._setup_barge_in()
        await self._open_stt()
        if self.carry is not None:  # what they said with the wake word goes first
            samples = self.carry.take()
            rate = int(getattr(self.carry, "sample_rate", STT_RATE) or STT_RATE)
            if len(samples):
                from littlebot.audio import dsp  # noqa: PLC0415

                await self.stt.feed(dsp.resample(np.asarray(samples, dtype=np.float32), rate, STT_RATE))
                log.info("assistant: %.1f s said with the wake word sent first", len(samples) / float(rate))
        self._spawn(self._pump())
        self._spawn(self._watch())
        self.connect_ms = int((self._now() - t0) * 1000)
        self.detail = "Listening."
        log.info("assistant: listening %d ms after the wake word (speech-to-text: %s)", self.connect_ms, self.stt_name)
        self._publish(ev.SESSION, {"active": True, "source": self.source})

    async def _warm(self) -> None:
        jobs = [self.svc.brain.warm()]
        warm_tts = getattr(self.svc.tts(), "warm", None)
        if warm_tts is not None:
            jobs.append(warm_tts())
        await asyncio.gather(*jobs, return_exceptions=True)

    async def _open_stt(self) -> None:
        provider = self.svc.stt_provider(exclude=self._failed_stt)
        if provider is None:
            raise SessionFailed("no_stt", "no speech-to-text is available")
        try:
            self.stt = await provider.open(self._on_stt)
        except Exception as e:  # noqa: BLE001 - SpeechError or anything else: the next one
            kind = str(getattr(e, "kind", "") or type(e).__name__)
            log.warning("assistant: speech-to-text %s didn't open (%s); trying the next", provider.name, kind)
            self._failed_stt.append(provider.name)
            return await self._open_stt()
        self.stt_name = str(provider.name)

    def _on_stt(self, event: STTEvent) -> None:
        """From the STT provider (any thread): into the session's queue."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if asyncio.get_running_loop() is loop:
                self._events.put_nowait(event)
                return
        except RuntimeError:
            pass
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._events.put_nowait, event)

    async def _pump(self) -> None:
        """The microphone, 16 kHz, into speech-to-text (the call audio's gate silences LittleBot's own voice)."""
        from littlebot.audio.dsp import StreamResampler  # noqa: PLC0415

        res = StreamResampler(48000, STT_RATE)  # LiveAudio.read_frame: 20 ms at 48 kHz
        while not self._ended.is_set():
            frame = await self.audio.read_frame()
            x = np.asarray(frame, dtype=np.float32)
            if frame.dtype == np.int16:
                x = x / 32768.0
            y = res.process(x)
            if len(y) and self.stt is not None:
                try:
                    await self.stt.feed(y)
                except Exception as e:  # noqa: BLE001 - the provider died: the next one takes over
                    await self._stt_failed(str(getattr(e, "kind", "") or type(e).__name__))

    async def _stt_failed(self, kind: str) -> None:
        old, self.stt = self.stt, None
        log.warning("assistant: speech-to-text %s failed (%s); switching", self.stt_name, kind)
        self._failed_stt.append(self.stt_name)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.aclose()
        try:
            await self._open_stt()
        except SessionFailed:
            self.error = "stt_failed"
            self.end("speech-to-text failed")

    # ------------------------------------------------------------ the main loop
    async def _main(self) -> None:
        while not self._ended.is_set():
            try:
                event = await asyncio.wait_for(self._events.get(), POLL_S * 4)
            except asyncio.TimeoutError:
                self._check_time()
                continue
            await self._on_event(event)
            self._check_time()

    def _busy(self) -> bool:
        return (self._answer is not None and not self._answer.done()) or self._hearing_since is not None

    def _check_time(self) -> None:
        now = self._now()
        if self._hearing_since is not None and now - self._hearing_since > self.cfg.max_turn_seconds + HEARING_GRACE_S:
            self._hearing_since = None
            if self.stt is not None:
                self._spawn(self.stt.finish())
        if now - self._started >= self.cfg.session_minutes * 60:
            self.end("time limit")
            return
        if self._busy() or getattr(self.audio, "speaking", False) or getattr(self.audio, "interrupted", False):
            self._deadline = max(self._deadline, now + 0.5)
            return
        if now >= self._deadline:
            self.end("no follow-up" if self.heard_user else "no words after the wake word")

    async def _on_event(self, event: STTEvent) -> None:
        audio = self.audio
        if event.kind == "start_of_turn":
            if self._hearing_since is None:
                self._hearing_since = self._now()
            if not self._answering():
                self._set_phase("hearing")
        elif event.kind == "partial":
            if event.text.strip() and getattr(audio, "interrupted", False):
                audio.barge_in_heard(event.text, False)  # their words: the held voice is dropped
            if self._hearing_since is None:
                self._hearing_since = self._now()
        elif event.kind == "end_of_turn":
            spoke_ms = int((self._now() - self._hearing_since) * 1000) if self._hearing_since is not None else 0
            self._hearing_since = None
            text = " ".join(event.text.split())
            if getattr(audio, "interrupted", False) and text:
                audio.barge_in_heard(text, True)
            if not text:
                if not self._answering():
                    self._set_phase("listening")
                return
            self._publish(ev.HEARD, {"words": len(text.split()), "ms": spoke_ms})
            self._publish(ev.CAPTION, {"who": "user", "text": text})
            self._start_answer(text)
        elif event.kind == "error":
            await self._stt_failed(event.detail or "error")

    def _answering(self) -> bool:
        return self._answer is not None and not self._answer.done()

    def _start_answer(self, text: str) -> None:
        if self._answering():
            voice = self._voice
            if voice is None or (voice.first_audio_at is None and not voice.heard):
                text = f"{self._answer_text} {text}".strip()  # they kept talking while it was thinking: one question
                log.info("assistant: more words before the answer started; asking again with them")
            else:
                self._record_cut()
            self._cut_answer()
        self._answer_text = text
        self._answer = self._spawn(self._respond(text))

    # ------------------------------------------------------------ one answer
    async def _respond(self, text: str) -> None:
        t0 = self._now()
        self.turns += 1
        self.heard_user = True
        self._set_phase("thinking")
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        box = self._toolbox = RobotToolbox(self.ctx, mode="chat", defer=True)
        voice = self._voice = VoiceOut(self.svc.tts(), self.audio, on_start=self._sentence_started, now=self._now)
        with contextlib.suppress(Exception):
            self.audio.unmute()
        said_by = "brain"
        result: Any = None
        try:
            local = await self._local_answer(text, box)
            if local is not None:
                said_by, words, end = local
                for s in _sentences(words):
                    voice.say(s)
                if end:
                    self._end_after = said_by
            else:
                result = await self.svc.brain.respond(text, history=self.svc.history.messages(), toolbox=box,
                                                      on_sentence=voice.say, on_tool=self._tool_started)
                if result.error and not result.sentences:
                    for s in _sentences(SORRY.get(result.error, SORRY_DEFAULT)):
                        voice.say(s)
            voice.close()
            await voice.done()
        except asyncio.CancelledError:
            voice.cancel()
            raise
        heard = " ".join(voice.heard) or (getattr(result, "text", "") or "")
        if heard and said_by != "stop":
            self.svc.history.add(text, heard)
            self._publish(ev.CAPTION, {"who": "robot", "text": heard})
        await self._after_turn(box)
        first = int((voice.first_audio_at - t0) * 1000) if voice.first_audio_at is not None else None
        self._publish(ev.TURN_DONE, {"ms_to_first_audio": first, "tools": list(getattr(result, "tools", []) or [])})
        if self.cfg.log_timings:
            log.info("assistant turn: by=%s stt=%s first_text_ms=%s first_audio_ms=%s total_ms=%d tools=%s tts=%s "
                     "model=%s%s", said_by, self.stt_name, getattr(result, "first_text_ms", None), first,
                     int((self._now() - t0) * 1000), ",".join(getattr(result, "tools", []) or []) or "-",
                     ",".join(voice.providers) or "-", getattr(result, "model", "") or "-",
                     f" error={result.error}" if result is not None and result.error else "")
        if self._end_after:
            self.end(f"said {self._end_after}")
            return
        follow = float(self.cfg.follow_up_seconds)
        if heard.rstrip().endswith("?"):
            follow = max(follow, QUESTION_FOLLOW_UP_S)
        if follow <= 0:
            self.end("one question per wake word")
            return
        self._deadline = self._now() + follow
        self._set_phase("listening")

    async def _local_answer(self, text: str, box: Any) -> tuple[str, str, bool] | None:
        """(who answered, words, end the session) without the brain: an instant intent, a voice game's move or a
        routine's phrase; None: the brain answers."""
        media = getattr(self.ctx, "media", None)
        st = _safe(media.status) if media is not None else None
        media_on = bool(st and st.get("state") in ("playing", "paused", "loading"))
        hit = intents.match(text, media_on=media_on)
        if hit is not None:
            words = await intents.run(hit, box, self.ctx)
            if hit.name == "stop":
                return "stop", "", True
            return f"intent:{hit.name}", words, hit.end
        hub = getattr(self.ctx, "skills", None)
        games = hub.get("voice_games") if hub is not None and hasattr(hub, "get") else None
        if games is not None and getattr(games, "active", None) is not None:
            try:
                reply = games.handle_utterance(text)
                if asyncio.iscoroutine(reply):
                    reply = await reply
            except Exception:  # noqa: BLE001 - the game's bug: the brain answers instead
                log.warning("assistant: the voice game failed on a move", exc_info=True)
                reply = None
            if reply is not None:
                card = getattr(reply, "card", None)
                if card and getattr(self.ctx, "screens", None) is not None:
                    self._spawn(_show(self.ctx, card))
                sound = getattr(reply, "sound", None)
                if sound:
                    self.play_sound(str(sound))
                return "game", str(getattr(reply, "say", "") or ""), False
        routines = hub.get("routines") if hub is not None and hasattr(hub, "get") else None
        if routines is not None and hasattr(routines, "match_phrase"):
            name = _safe(lambda: routines.match_phrase(text))
            if name:
                log.info("assistant: routine %r matched", name)
                return None  # the brain runs it (its context note says how), with the tools it needs
        return None

    async def _after_turn(self, box: Any) -> None:
        pending = list(getattr(box, "pending", []) or [])
        if not pending:
            return
        later = [p for p in pending if getattr(p, "takeover", True)]
        box.pending = [p for p in pending if not getattr(p, "takeover", True)]
        if box.pending:
            await box.run_pending()
        if later:
            self._handover = later
            self.end("handing over to " + ", ".join(getattr(p, "label", "?") for p in later))

    def _sentence_started(self, s: Sentence) -> None:
        self._set_phase("speaking")
        face = self.ctx.face
        if face is not None and self.cfg.expressions:
            with contextlib.suppress(Exception):
                if s.expression:
                    face.set_expression(s.expression, hold=EXPRESSION_HOLD_S)
                if s.animation:
                    face.play_animation(s.animation)
        self._publish(ev.SENTENCE, {"index": s.index, "chars": len(s.text), "expression": s.expression,
                                    "last": False})

    def _tool_started(self, name: str) -> None:
        if self.cfg.thinking_sound:
            self._spawn(self._thinking_while(self._voice))

    async def _thinking_while(self, voice: VoiceOut | None) -> None:
        """The soft thinking sound while a tool runs and nothing else is heard."""
        await asyncio.sleep(THINKING_AFTER_S)
        while voice is self._voice and self._answering() and not self._ended.is_set():
            if voice is not None and voice.busy and getattr(self.audio, "queued_s", 0.0) > 0:
                return
            if not getattr(self.audio, "speaking", False):
                with contextlib.suppress(Exception):
                    self.audio.play_thinking()
            await asyncio.sleep(THINKING_REPEAT_S)

    def play_sound(self, name: str) -> None:
        """A sound effect ("tada", "uhoh" ...) through the call's own speaker stream."""
        hit = None
        with contextlib.suppress(Exception):
            hit = self.ctx.audio.sound_samples(name)
        if hit is not None and self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.play_pcm(hit[0], int(hit[1]))

    def announce(self, text: str) -> None:
        """Say ``text`` now in this session (a reminder rang, a game's tap was judged): after what is being said."""
        if self._ended.is_set() or not text.strip():
            return
        self._spawn(self._say_now(text))

    async def _say_now(self, text: str) -> None:
        while self._answering():
            await asyncio.sleep(0.1)
        voice = self._voice = VoiceOut(self.svc.tts(), self.audio, on_start=self._sentence_started, now=self._now)
        with contextlib.suppress(Exception):
            self.audio.unmute()
        for s in _sentences(text):
            voice.say(s)
        voice.close()
        await voice.done()
        self.svc.history.add("(LittleBot spoke up by itself)", text)
        self._deadline = max(self._deadline, self._now() + max(4.0, float(self.cfg.follow_up_seconds)))

    def _record_cut(self) -> None:
        voice = self._voice
        if voice is not None and voice.heard:
            self.svc.history.add(self._answer_text, " ".join(voice.heard) + " (interrupted)")

    def _cut_answer(self) -> None:
        """Stop the answer being thought of or said (they cut in, or added to the question)."""
        if self._voice is not None:
            self._voice.cancel()
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        self._answer = None

    # ------------------------------------------------------------ barge-in
    def _setup_barge_in(self) -> None:
        setup = getattr(self.audio, "setup_barge_in", None)
        if not callable(setup):
            return
        hooks: dict[str, Any] = {"on_change": self._barge_in_changed,
                                 "identify": getattr(getattr(self.ctx, "voices", None), "identify", None)}
        if getattr(self.audio, "barge_in_on", False) and getattr(self.ctx.config.chatgpt, "live_stop_words", False):
            try:
                from littlebot.chatgpt.live_audio import RATE  # noqa: PLC0415
                from littlebot.chatgpt.stop_words import StopWords  # noqa: PLC0415

                hooks["stop_words"] = StopWords.from_model(getattr(self.ctx.config.wake, "model_path", None),
                                                           rate=RATE)
            except Exception:  # noqa: BLE001 - stop words are optional
                log.debug("assistant: no stop words", exc_info=True)
        hooks["voice_said"] = lambda: " ".join((self._voice.heard if self._voice is not None else [])[-2:])
        with contextlib.suppress(TypeError):
            setup(**hooks)

    def _barge_in_changed(self, what: str, info: dict[str, Any]) -> None:
        if self._ended.is_set():
            return
        if what == "interrupted":
            self._set_phase("hearing")
        elif what == "heard":  # their words confirmed it: the answer being said is dropped
            self.barge_ins += 1
            self._record_cut()
            self._cut_answer()
            self._publish(ev.INTERRUPTED, {})
            log.info("assistant: interrupted; listening to them")
        elif what == "stop_word":
            self.barge_ins += 1
            self._record_cut()
            self._cut_answer()
            with contextlib.suppress(Exception):
                self.audio.drop_voice()
            self._publish(ev.INTERRUPTED, {"stop": True})
            self._deadline = self._now() + max(2.0, float(self.cfg.follow_up_seconds))
            self._set_phase("listening")
            log.info("assistant: '%s': quiet now", info.get("said") or "stop")

    # ------------------------------------------------------------ the face and the dashboard
    async def _watch(self) -> None:
        """The mouth follows the voice; the phase follows the speaker; a stalled device is reopened."""
        last: tuple[float, str] | None = None
        sent = 0.0
        while not self._ended.is_set():
            now = self._now()
            speaking = bool(getattr(self.audio, "speaking", False))
            mouth = getattr(self.audio, "mouth", None) if speaking else (0.0, "closed")
            if mouth is None:
                mouth = (min(1.0, float(getattr(self.audio, "level", 0.0)) * 8.0), "")
            mouth = (round(float(mouth[0]), 2), str(mouth[1]))
            if mouth != last or (speaking and now - sent >= 0.15):
                last, sent = mouth, now
                self._publish(Topics.AUDIO_VISEME, {"level": mouth[0], **({"shape": mouth[1]} if mouth[1] else {})})
            if not speaking and self.phase == "speaking" and not self._answering():
                self._set_phase("listening")
            stalled = getattr(self.audio, "stalled", None)
            which = stalled() if callable(stalled) else None
            if which is not None:
                log.warning("assistant: the %s stopped; opening the speaker and microphone again", which)
                try:
                    await asyncio.to_thread(self.audio.reopen)
                except Exception:  # noqa: BLE001
                    self.error = "audio"
                    self.end(f"the {which} stopped")
                    return
            await asyncio.sleep(POLL_S)

    def _set_phase(self, phase: str) -> None:
        if phase == self.phase:
            return
        self.phase = phase
        self.detail = {"listening": "Listening.", "hearing": "Hearing you.", "thinking": "Thinking...",
                       "speaking": "Talking."}.get(phase, self.detail)
        self._publish(ev.PHASE, {"phase": phase})
        state = {"speaking": RobotState.SPEAKING, "thinking": RobotState.THINKING}.get(phase, RobotState.LISTENING)
        sm = self.ctx.state
        if sm.state == state:
            self._our_state = state
        elif sm.transition(state, "assistant"):
            self._our_state = state

    def _publish(self, topic: str, data: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.ctx.bus.publish(topic, data, source=OWNER)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("assistant: a session task failed", exc_info=task.exception())
            if task is not self._answer:
                self.error = self.error or type(task.exception()).__name__
                self.end("failed")

    # ------------------------------------------------------------ cleanup
    async def _cleanup(self) -> None:
        self._ended.set()
        if self._voice is not None:
            self._voice.cancel()
        if self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.mute()
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.stt is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.stt.aclose(), 3.0)
        if self.audio is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.audio.close)
        if self._suspended:
            with contextlib.suppress(Exception):
                self.ctx.audio.resume("assistant")
        self._publish(Topics.AUDIO_VISEME, {"level": 0.0})
        if self.lease is not None:
            self.lease.release()
        current = self.ctx.state.state
        if self._our_state is not None and current in (RobotState.LISTENING, RobotState.SPEAKING, RobotState.THINKING):
            self.ctx.state.release(current, "assistant session ended")
        if self.carry is not None:
            self.carry.drop(self.end_reason or "session ended")
        self.end_reason = self.end_reason or ("failed" if self.error else "ended")
        self.phase = "ended"
        self._publish(ev.SESSION, {"active": False, "reason": self.end_reason[:60]})
        log.info("assistant: session ended (%s) after %.1f s, %d turn(s)", self.end_reason,
                 self._now() - self._started, self.turns)

    async def run_handover(self) -> list[str]:
        """The take-overs asked for in the last turn (a video, an activity, sleep), once everything is handed back."""
        if not self._handover:
            return []
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        box = RobotToolbox(self.ctx, mode="chat")
        box.pending, self._handover = self._handover, []
        return await box.run_pending()


def lease_for(svc: Any, end: Any) -> Any:
    """The session's lease: face, speaker and microphone at the parent's priority (as GPT-Live)."""
    return svc.ctx.arbiter.acquire(OWNER, [Resource.FACE, Resource.AUDIO_OUT, Resource.MICROPHONE], Priority.PARENT,
                                   on_preempt=lambda _lease, why: end(f"stopped ({why})"))


def _sentences(text: str) -> list[Sentence]:
    from littlebot.assistant.sentences import SentenceSplitter  # noqa: PLC0415

    sp = SentenceSplitter()
    return [*sp.feed(text), *sp.flush()]


async def _show(ctx: Any, card: dict[str, Any]) -> None:
    with contextlib.suppress(Exception):
        await ctx.screens.show(card, source="assistant")


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


__all__ = ["OWNER", "SessionFailed", "VoiceSession", "lease_for"]
