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
import collections
import contextlib
import logging
import re
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
QUESTION_FOLLOW_UP_S = 10.0  # LittleBot asked them something: wait at least this long for the answer
HEARD_KEEP_S = 12.0  # the microphone kept (in memory only) to recognise whose voice a turn was
IDENTIFY_TIMEOUT_S = 3.0
SPECULATION_MAX_S = 8.0  # an early answer whose turn is neither confirmed nor resumed by then is dropped
# Instant intents that steer Spotify when it is what's playing on LittleBot (skills/spotify.py command()).
SPOTIFY_ACTIONS = {"pause": "pause", "resume": "resume", "skip": "next", "stop_media": "stop"}
STT_REOPENS = 3  # speech-to-text that fails this often in one session ends it
HEARING_GRACE_S = 2.0  # a turn that never ends (noise): finished after max_turn_seconds + this
ECHO_MEMORY_S = 20.0  # what LittleBot said this recently may come back through the microphone
ECHO_SHARE = 0.8  # a turn whose words are mostly LittleBot's own recent words is its echo, not them
PAUSE_WORDS = frozenset({"wait", "wait a second", "wait a minute", "hold on", "hang on", "pause"})
NO_WORDS_TURN_S = 4.0  # a turn with no words at all after this long was a noise: listening goes on as before
CUT_IN_NOTE_S = 15.0  # a turn this soon after they talked over the voice is for LittleBot (_turn_notes)
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
        self._cut_in_at: float | None = None  # when they last talked over the voice (their next turn is for it)
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
        self._words_at: float | None = None  # the latest partial with words in the turn being heard
        self._said: collections.deque[tuple[float, str]] = collections.deque(maxlen=12)  # its own recent sentences
        self._deadline = self._started + self.cfg.first_words_seconds
        self._end_after: str | None = None  # end once the current answer was heard (said "bye")
        self._quiet_end = False  # the brain called end_conversation: nothing more is said, and the session ends
        self._handover: list[Any] = []  # take-overs to run after the session (a video, an activity ...)
        self._suspended = False
        self._our_state: RobotState | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._failed_stt: list[str] = []
        self._stt_chain: Any = None
        self._gate: asyncio.Event | None = None  # an early answer's: set once their turn really ended
        self._confirmed_at: float | None = None
        self._spec_at = 0.0
        self._mouths: list[VoiceOut] = []  # every VoiceOut made in this session (only the newest may talk)
        self.mouth_frames = 0  # lip-sync frames sent to the face in the current turn (its log line says how many)
        self._unsaid: list[str] = []  # announcements not said yet (busy until they are)
        self._saying = False
        # Who is talking (voice recognition, opt-in) and who is in view (face recognition, opt-in): for the brain.
        self.who: Any = None  # assistant.who.WhoTracker, made when voice or face recognition can be used
        self._claim: Any = None  # "I'm Ali" in the turn being recognised: its voiceprint is kept for Ali
        self._heard_audio: collections.deque[np.ndarray] = collections.deque()  # the last HEARD_KEEP_S, 16 kHz
        self._heard_n = 0
        self._faces: Any = None  # vision.faces.CallFaces while face recognition can look

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
        self._set_phase("listening")
        self.audio = self.svc.audio_factory()
        early = False
        if self.carry is not None and hasattr(audio_svc, "suspend"):
            # The call audio opens while the wake word still listens (PipeWire shares the microphone), so nothing
            # said between the wake word letting go and the call's microphone opening (~150 ms) is lost.
            try:
                await asyncio.to_thread(self.audio.open, self._loop)
                early = True
            except Exception as e:  # noqa: BLE001 - a device only one stream may open: the old order
                log.info("assistant: the call audio didn't open beside the wake word (%s); opening it after",
                         type(e).__name__)
                with contextlib.suppress(Exception):
                    self.audio.close()
                self.audio = self.svc.audio_factory()
        t_audio = self._now()
        if hasattr(audio_svc, "suspend"):
            await audio_svc.suspend("assistant")
            self._suspended = True
            flush = getattr(self.audio, "flush_input", None)
            if early and flush is not None:
                flush()  # the wake word's carry has what was said until now
        t_lent = self._now()
        if not early:
            await asyncio.to_thread(self.audio.open, self._loop)
            t_audio = self._now()
        self._log_devices()
        self._setup_barge_in()
        await self._open_stt()
        t_stt = self._now()
        self._who_woke()
        self._watch_faces()
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
        audio_ms, lent_ms = ((t_audio - t0, t_lent - t_audio) if early else (t_audio - t_lent, t_lent - t0))
        log.info("assistant: listening %d ms after the wake word (speech-to-text: %s; call audio %d ms%s, devices lent "
                 "%d ms, speech-to-text %d ms)", self.connect_ms, self.stt_name, int(audio_ms * 1000),
                 " first" if early else "", int(lent_ms * 1000), int((t_stt - max(t_audio, t_lent)) * 1000))
        self._publish(ev.SESSION, {"active": True, "source": self.source})

    def _log_devices(self) -> None:
        """Which microphone and speaker the session opened, and at what volume: the first thing to check when it
        talks (the face moves) but nothing is heard."""
        names = getattr(self.audio, "device_names", ("", ""))
        try:
            volume = float(self.ctx.settings.get("audio.volume", self.ctx.config.audio.speaker.volume))
        except (TypeError, ValueError):
            volume = -1.0
        log.info("assistant: microphone %s, speaker %s, volume %d%%%s", names[0] or "?", names[1] or "?",
                 round(volume * 100), " (TOO LOW TO HEAR: say 'volume 60')" if 0 <= volume < 0.08 else "")

    async def _warm(self) -> None:
        jobs = [self.svc.brain.warm()]
        warm_tts = getattr(self.svc.tts(), "warm", None)
        if warm_tts is not None:
            jobs.append(warm_tts())
        await asyncio.gather(*jobs, return_exceptions=True)

    async def _open_stt(self) -> None:
        """Open speech-to-text (the chain tries its providers in order; one that failed lately is skipped)."""
        if self._stt_chain is None:
            self._stt_chain = self.svc.stt_provider()
        chain = self._stt_chain
        if chain is None:
            raise SessionFailed("no_stt", "no speech-to-text is available")
        try:
            self.stt = await chain.open(self._on_stt)
        except Exception as e:  # noqa: BLE001 - SpeechError: none of them opened
            raise SessionFailed("stt_failed", str(getattr(e, "kind", "") or type(e).__name__)) from None
        self.stt_name = str(getattr(chain, "last_provider", None) or chain.name)

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
            if len(y) and self._voices() is not None:
                self._keep_heard(y)
            if len(y) and self.stt is not None:
                try:
                    await self.stt.feed(y)
                except Exception as e:  # noqa: BLE001 - the provider died: the next one takes over
                    await self._stt_failed(str(getattr(e, "kind", "") or type(e).__name__))

    async def _stt_failed(self, kind: str) -> None:
        if self._speculating():
            self._cut_answer(silence=True)  # its turn will never be confirmed now
        old, self.stt = self.stt, None
        self._failed_stt.append(self.stt_name)
        log.warning("assistant: speech-to-text %s failed (%s); switching", self.stt_name, kind)
        if old is not None:
            with contextlib.suppress(Exception):
                await old.aclose()
        if len(self._failed_stt) > STT_REOPENS:
            self.error = "stt_failed"
            self.end("speech-to-text kept failing")
            return
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
        return (self._answer is not None and not self._answer.done()) or self._hearing_since is not None or \
            bool(self._unsaid) or self._saying

    def _check_time(self) -> None:
        now = self._now()
        if self._speculating() and now - self._spec_at > SPECULATION_MAX_S:
            self._cut_answer()  # neither confirmed nor dropped by speech-to-text: never said
        if self._hearing_since is not None and self._words_at is None and \
                now - self._hearing_since > NO_WORDS_TURN_S:
            # speech-to-text started a turn on a noise and never ended it (seen with Flux on the robot, 2026-10-30):
            # no words came, so nobody is talking; listening as before (a real turn still ends with its words)
            log.info("assistant: a turn started %.1f s ago but no words came; not waiting for it",
                     now - self._hearing_since)
            self._hearing_since = None
            if not self._answering():
                self._set_phase("listening")
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
                self._words_at = None
            if not self._answering():
                self._set_phase("hearing")
        elif event.kind == "partial":
            if event.text.strip() and getattr(audio, "interrupted", False) and not self._echo_of_me(event.text):
                audio.barge_in_heard(event.text, False)  # their words: the held voice is dropped
            if self._hearing_since is None:
                self._hearing_since = self._now()
            if event.text.strip():
                self._words_at = self._now()
        elif event.kind == "eager_end_of_turn":
            self._speculate(" ".join(event.text.split()))
        elif event.kind == "turn_resumed":
            if self._speculating():
                log.debug("assistant: they went on talking; the early answer is dropped")
                self._cut_answer()
                self._set_phase("hearing")
        elif event.kind == "end_of_turn":
            spoke_ms = int((self._now() - self._hearing_since) * 1000) if self._hearing_since is not None else 0
            self._hearing_since = self._words_at = None
            text = " ".join(event.text.split())
            if text and self._echo_of_me(text):
                log.info("assistant: that turn was its own voice heard back (%d words); ignored", len(text.split()))
                if not self._answering():
                    self._set_phase("listening")
                return
            if text:
                log.info("assistant: their turn ended (%d words, %d ms from its start)", len(text.split()), spoke_ms)
            if getattr(audio, "interrupted", False) and text:
                audio.barge_in_heard(text, True)
            if self._speculating():
                if text and _same_words(text, self._answer_text):
                    self._confirmed_at = self._now()
                    self._gate.set()  # the early answer was right: it is said now
                    self._publish(ev.HEARD, {"words": len(text.split()), "ms": spoke_ms})
                    self._publish(ev.CAPTION, {"who": "user", "text": text})
                    self._identify_turn(spoke_ms, text)
                    return
                self._cut_answer()  # their words changed: answer the final ones
            if not text:
                if not self._answering():
                    self._set_phase("listening")
                return
            self._publish(ev.HEARD, {"words": len(text.split()), "ms": spoke_ms})
            self._publish(ev.CAPTION, {"who": "user", "text": text})
            self._identify_turn(spoke_ms, text)
            self._start_answer(text)
        elif event.kind == "error":
            await self._stt_failed(event.detail or "error")

    def _answering(self) -> bool:
        return self._answer is not None and not self._answer.done()

    def _echo_of_me(self, text: str) -> bool:
        """``text`` is mostly LittleBot's own recent words (its voice leaking back into the microphone), not theirs.
        Only for 3 words or more: "stop", "yes", "next one" are always theirs."""
        words = _words(text)
        if len(words) < 3:
            return False
        now = self._now()
        said = set()
        for at, sentence in self._said:
            if now - at <= ECHO_MEMORY_S:
                said |= set(_words(sentence))
        if not said:
            return False
        return sum(1 for w in words if w in said) / len(words) >= ECHO_SHARE

    def _remember_said(self, s: Sentence) -> None:
        self._said.append((self._now(), s.text))
    def _speculating(self) -> bool:
        """An early answer (started on "probably done") is waiting for their turn to really end."""
        return self._answering() and self._gate is not None and not self._gate.is_set()

    def _speculate(self, text: str) -> None:
        """Flux thinks they are probably done: start the brain now; it says nothing and runs no tool until the turn
        really ends with the same words (``end_of_turn``), and is dropped if they go on (``turn_resumed``)."""
        if not text or getattr(self.audio, "interrupted", False) or getattr(self.audio, "speaking", False):
            return
        if self._answering() and not self._speculating():
            return  # an answer they are waiting for is on its way
        if not self._speculable(text):
            return
        if self._speculating():
            self._cut_answer()
        self._gate = asyncio.Event()
        self._spec_at = self._now()
        self._voice = None
        self._answer_text = text
        self._answer = self._spawn(self._respond(text, gate=self._gate))

    def _media_on(self) -> tuple[bool, Any]:
        """(a video or YouTube music is on, the Spotify skill when Spotify plays or is paused on LittleBot)."""
        media = getattr(self.ctx, "media", None)
        st = _safe(media.status) if media is not None else None
        video_on = bool(st and st.get("state") in ("playing", "paused", "loading"))
        hub = getattr(self.ctx, "skills", None)
        sp = hub.get("spotify") if hub is not None and hasattr(hub, "get") else None
        spotify_on = sp is not None and bool(_safe(sp.playing) or _safe(sp.paused))
        return video_on, (sp if spotify_on else None)

    def _speculable(self, text: str) -> bool:
        """Only the brain's answers start early (an instant intent or a game's move is quicker than that anyway)."""
        video_on, spotify = self._media_on()
        if intents.match(text, media_on=video_on or spotify is not None):
            return False
        hub = getattr(self.ctx, "skills", None)
        games = hub.get("voice_games") if hub is not None and hasattr(hub, "get") else None
        if games is not None and getattr(games, "active", None) is not None:
            return False
        rem = hub.get("reminders") if hub is not None and hasattr(hub, "get") else None
        return not (rem is not None and _safe(rem.ringing))

    def _start_answer(self, text: str) -> None:
        if self._answering():
            voice = self._voice
            if voice is None or (voice.first_audio_at is None and not voice.heard):
                text = f"{self._answer_text} {text}".strip()  # they kept talking while it was thinking: one question
                log.info("assistant: more words before the answer started; asking again with them")
            else:
                self._record_cut()
            self._cut_answer(silence=voice is not None and voice.first_audio_at is not None)
        self._answer_text = text
        self._gate = None
        self._voice = None  # until the new answer runs: more words now are added to this question
        self._answer = self._spawn(self._respond(text))

    # ------------------------------------------------------------ one answer
    async def _respond(self, text: str, *, gate: asyncio.Event | None = None) -> None:
        """Answer ``text``. ``gate``: an early answer (``_speculate``): nothing is said and no tool runs until it
        is set."""
        t0 = self._now()
        if gate is None:
            self.turns += 1
            self.heard_user = True
        self._set_phase("thinking")
        from littlebot.chatgpt.tools import RobotToolbox  # noqa: PLC0415

        box = self._toolbox = RobotToolbox(self.ctx, mode="chat", defer=True)
        voice = self._voice = self._new_voice()
        held: list[Sentence] = []

        def say(s: Sentence) -> None:
            if self._quiet_end:
                return  # told to stop: not a word more
            if gate is None or gate.is_set():
                while held:
                    voice.say(held.pop(0))
                voice.say(s)
            else:
                held.append(s)

        async def opened() -> None:
            if gate is not None:
                await gate.wait()
                self.turns += 1
                self.heard_user = True
                t_heard[0] = self._confirmed_at or self._now()
                while held:
                    voice.say(held.pop(0))

        t_heard = [t0]
        waiter = self._spawn(opened()) if gate is not None else None
        said_by = "brain"
        result: Any = None
        try:
            # an early answer never takes a local path: those act at once (a game starts, an alarm snoozes)
            local = await self._local_answer(text, box) if gate is None else None
            if local is not None:
                said_by, words, end = local
                for s in _sentences(words):
                    say(s)
                if end:
                    self._end_after = said_by
            else:
                tools = box if gate is None else _GatedToolbox(box, gate)
                result = await self.svc.brain.respond(text, history=self.svc.history.messages(), toolbox=tools,
                                                      on_sentence=say, on_tool=self._tool_started, mark_day=False,
                                                      extra=[*self.who_notes(), *self._turn_notes(gate)],
                                                      extra_tools=self._session_tools())
                if result.error and not result.sentences:
                    for s in _sentences(SORRY.get(result.error, SORRY_DEFAULT)):
                        say(s)
            if waiter is not None:
                await waiter  # an early answer waits here until their turn really ended
            voice.close()
            await voice.done()
            if voice.first_audio_at is None and getattr(self.audio, "interrupted", False):
                with contextlib.suppress(Exception):
                    self.audio.drop_voice()  # nothing was said after they cut in: the call audio listens normally
        except asyncio.CancelledError:
            voice.cancel()
            if waiter is not None:
                waiter.cancel()
            raise
        t0 = t_heard[0]
        heard = " ".join(voice.heard) or (getattr(result, "text", "") or "")
        if voice.heard and result is not None:
            with contextlib.suppress(Exception):
                self.svc.brain.talked_today()
        if heard and said_by != "stop":
            self.svc.history.add(text, heard)
            self._publish(ev.CAPTION, {"who": "robot", "text": heard})
        await self._after_turn(box)
        first = int((voice.first_audio_at - t0) * 1000) if voice.first_audio_at is not None else None
        self._publish(ev.TURN_DONE, {"ms_to_first_audio": first, "tools": list(getattr(result, "tools", []) or [])})
        if self.cfg.log_timings:
            log.info("assistant turn: by=%s stt=%s first_text_ms=%s first_audio_ms=%s total_ms=%d tools=%s tts=%s "
                     "model=%s mouth_frames=%d quiet_cut_ms=%d%s", said_by, self.stt_name,
                     getattr(result, "first_text_ms", None), first, int((self._now() - t0) * 1000),
                     ",".join(getattr(result, "tools", []) or []) or "-", ",".join(voice.providers) or "-",
                     getattr(result, "model", "") or "-", self.mouth_frames, int(getattr(voice, "cut_s", 0.0) * 1000),
                     f" error={result.error}" if result is not None and result.error else "")
        self.mouth_frames = 0
        if self._quiet_end:
            self.end("asked to stop")
            return
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
        alarm = self._alarm_answer(text)
        if alarm is not None:
            return "alarm", alarm, False
        video_on, spotify = self._media_on()
        hit = intents.match(text, media_on=video_on or spotify is not None)
        if hit is not None:
            if spotify is not None and not video_on and hit.name in SPOTIFY_ACTIONS:
                try:  # "pause", "next", "carry on" while Spotify plays on LittleBot: no model, no video tools
                    done, sentence = await spotify.command(SPOTIFY_ACTIONS[hit.name])
                except Exception:  # noqa: BLE001
                    done, sentence = False, "I couldn't reach Spotify just now."
                return f"intent:{hit.name}", "" if done else str(sentence or ""), hit.end
            words = await intents.run(hit, box, self.ctx)
            if hit.name == "stop":
                return "stop", "", True
            return f"intent:{hit.name}", words, hit.end
        hub = getattr(self.ctx, "skills", None)
        games = hub.get("voice_games") if hub is not None and hasattr(hub, "get") else None
        if games is not None:  # every turn: a move, or "let's play trivia" / "play again" (cheap when no game runs)
            self._tool_started("voice_game")  # a free-topic trivia round takes a few seconds to write: a soft sound
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
                    self.play_sound(str(sound))  # before the words
                return "game", str(getattr(reply, "say", "") or ""), False
        routines = hub.get("routines") if hub is not None and hasattr(hub, "get") else None
        if routines is not None and hasattr(routines, "match_phrase"):
            name = _safe(lambda: routines.match_phrase(text))
            if name:
                log.info("assistant: routine %r matched", name)
                return None  # the brain runs it (its context note says how), with the tools it needs
        return None

    # ------------------------------------------------------------ who is talking, who is in view
    def _voices(self) -> Any:
        """``ctx.voices`` while voice recognition may listen (the parent's switch, the model), else None."""
        voices = getattr(self.ctx, "voices", None)
        try:
            return voices if voices is not None and voices.blocked_reason() is None else None
        except Exception:  # noqa: BLE001
            return None

    def _keep_heard(self, y: np.ndarray) -> None:
        self._heard_audio.append(y)
        self._heard_n += len(y)
        while self._heard_audio and self._heard_n - len(self._heard_audio[0]) >= HEARD_KEEP_S * STT_RATE:
            self._heard_n -= len(self._heard_audio.popleft())

    def _who_woke(self) -> None:
        """Whose voice said "Hey LittleBot" (the wake word keeps it only while voice recognition is on)."""
        carry = self.carry
        if carry is None or self._voices() is None:
            return
        try:
            samples = carry.voice_audio()
            carry.forget_voice()
        except Exception:  # noqa: BLE001
            return
        if len(samples):
            self._spawn(self._identify(samples, int(getattr(carry, "sample_rate", STT_RATE) or STT_RATE)))

    def _tracker(self) -> Any:
        if self.who is None:
            from littlebot.assistant.who import WhoTracker  # noqa: PLC0415

            voices = getattr(self.ctx, "voices", None)
            child = _child_name(self.ctx)

            def is_child(name: str, role: str) -> bool:
                try:
                    from littlebot.vision.faces import is_child as _is_child  # noqa: PLC0415

                    return bool(_is_child(name, role, child))
                except Exception:  # noqa: BLE001
                    return False

            def lookup(person_id: int) -> tuple[str, str] | None:
                try:
                    row = voices.store.people.person(person_id)
                except Exception:  # noqa: BLE001
                    return None
                return (str(row["name"]), str(row.get("role") or "")) if row else None

            threshold = float(getattr(voices, "threshold", 0.5) or 0.5) if voices is not None else 0.5
            self.who = WhoTracker(threshold=threshold, is_child=is_child, lookup=lookup, now=self._now)
        return self.who

    def _identify_turn(self, spoke_ms: int, text: str = "") -> None:
        """Whose voice that turn was (in the background: the answer never waits for it; the next one knows).
        "I'm Ali" in it: that voiceprint is kept for Ali (learning in a chat)."""
        voices = self._voices()
        if voices is None or not self._heard_audio:
            return
        self._claim = _claim(self.ctx, voices, text) if text else None
        audio = np.concatenate(list(self._heard_audio))
        keep = int(min(HEARD_KEEP_S, max(1.0, spoke_ms / 1000 + 0.6)) * STT_RATE)
        self._spawn(self._identify(audio[-keep:], STT_RATE, learn=True))

    async def _identify(self, samples: np.ndarray, rate: int, *, learn: bool = False) -> None:
        voices = self._voices()
        if voices is None:
            return
        claim, self._claim = (self._claim, None) if learn else (None, self._claim)
        try:
            heard = await asyncio.wait_for(voices.analyse(samples, rate, one_voice=learn), IDENTIFY_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - optional: the brain just doesn't know who
            log.debug("assistant: voice recognition failed", exc_info=True)
            return
        finally:
            del samples
        before = self._tracker().current()
        who = self._tracker().voice(heard)
        if who is not None and (before is None or before.person_id != who.person_id or before.sure != who.sure):
            log.info("assistant: talking with %s%s (by %s)", "" if who.sure else "probably ", who.label, who.how)
        if not learn:
            return
        try:
            if claim is not None and claim.person is not None:
                res = voices.keep_claimed(int(claim.person["id"]), heard)
                log.info("assistant: they said who they are; voiceprint %s (%s)",
                         "kept" if res.get("ok") else "not kept", res.get("reason"))
                if res.get("ok") or res.get("reason") in ("too_short", "nobody"):
                    p = claim.person
                    self._tracker().said(int(p["id"]), str(p["name"]), str(p.get("role") or ""))
            else:
                voices.improve(heard)  # very sure: its voiceprint sharpens theirs (rate-limited by the service)
        except Exception:  # noqa: BLE001 - learning is a bonus
            log.debug("assistant: learning a voice failed", exc_info=True)

    def _watch_faces(self) -> None:
        """Who is in front of the camera during the session (opt-in face recognition, on the robot)."""
        try:
            from littlebot.vision import faces  # noqa: PLC0415

            recognition = faces.get_faces(self.ctx)
            if recognition.blocked_reason() in ("off", "no_camera", "camera_off"):
                return
            self._faces = faces.CallFaces(recognition, tell=lambda _text: False,
                                          hide=faces.hidden_child_name(self.ctx), now=self._now)
            self._spawn(self._faces.run())
        except Exception:  # noqa: BLE001 - knowing who is there must never stop the session
            log.debug("assistant: face recognition unavailable", exc_info=True)
            self._faces = None

    def who_notes(self) -> list[str]:
        """For the brain's context: who is talking (voice and face together) and who is in view. Names only."""
        notes: list[str] = []
        faces = self._faces
        if faces is not None:
            here = list(getattr(getattr(faces, "presence", None), "here", {}).values())
            self._tracker().faces(here)
        who = self.who.current() if self.who is not None else None
        if who is not None:
            notes.append(who.note())
        if faces is not None:
            seen = _safe(faces.summary) or ""
            if seen and seen not in ("off", "nobody it knows"):
                notes.append(f"in front of your camera: {seen} (recognised by face)")
        return notes

    def _turn_notes(self, gate: asyncio.Event | None) -> list[str]:
        """For the brain: when what they said is certainly for LittleBot (so never [silent]). On the robot
        (2026-10-31) words said right after cutting in were twice taken for people talking to each other."""
        first = self.turns + (1 if gate is not None else 0) == 1
        if self._cut_in_at is not None and self._now() - self._cut_in_at < CUT_IN_NOTE_S:
            return ["they just talked over you to say this: it is for you; answer it (if it's a new question, "
                    "answer that one)"]
        if first:
            return ["they woke you with your name to say this: it is for you; answer it"]
        return []

    def _session_tools(self) -> dict[str, Any]:
        return {"end_conversation": ({"name": "end_conversation", "final": True,
                                      "description": "End the conversation now, without a word: when they tell you "
                                                     "to stop (stop talking, be quiet, that's enough, end the call, "
                                                     "go away). Say nothing before or after it.",
                                      "parameters": {"type": "object", "properties": {},
                                                     "additionalProperties": False}},
                                     self._end_quietly),
                "who_is_here": ({"name": "who_is_here",
                                 "description": "Who is talking and who is in front of LittleBot's camera, from voice "
                                                "and face recognition on the robot (a fresh look). For 'do you know "
                                                "who I am?', 'who's here?', 'can you see me?'. Never guess a name "
                                                "it doesn't give.",
                                 "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
                                self._who_is_here)}

    async def _end_quietly(self, _args: dict[str, Any]) -> str:
        """end_conversation: whatever was about to be said is dropped; the session ends once this turn is over."""
        self._quiet_end = True
        voice = self._voice
        if voice is not None:
            voice.cancel()
        with contextlib.suppress(Exception):
            self.audio.drop_voice()
        log.info("assistant: asked to stop; ending without a word")
        return "ok: the conversation is over; say nothing"

    async def _who_is_here(self, _args: dict[str, Any]) -> str:
        parts: list[str] = []
        faces = self._faces
        if faces is not None:
            try:
                parts.append("Camera: " + str(await faces.answer_now()))
            except Exception:  # noqa: BLE001
                parts.append("Camera: face recognition couldn't look just now.")
        else:
            parts.append("Camera: face recognition is off or there is no camera.")
        who = self.speaker
        if self._voices() is None:
            parts.append("Voice: voice recognition is off (a grown-up can switch it on in the dashboard's Privacy page "
                         "and record their voice under Who's who).")
        elif who is None:
            parts.append("Voice: not sure whose voice it is yet (they can say 'I'm <name>' to teach LittleBot).")
        else:
            parts.append(f"Voice: {'' if who.sure else 'probably '}{who.label} (by {who.how}).")
        return " ".join(parts)

    @property
    def speaker(self) -> Any:
        """Who is talking now (``assistant.who.Who``), or None."""
        return self.who.current() if self.who is not None else None

    def _alarm_answer(self, text: str) -> str | None:
        """An alarm or reminder is ringing: "stop" / "okay" stops it, "snooze" snoozes it (no model needed)."""
        hub = getattr(self.ctx, "skills", None)
        rem = hub.get("reminders") if hub is not None and hasattr(hub, "get") else None
        if rem is None or not _safe(rem.ringing):
            return None
        if SNOOZE.match(text):
            done = _safe(rem.snooze) or []
            minutes = _safe(rem.snooze_minutes) or 9
            return f"Snoozed for {minutes} minutes." if done else None
        if intents.STOP.match(text) or STOP_ALARM.match(text) or intents.THANKS.match(text):
            return "Okay, it's off." if _safe(rem.stop_ringing) else None
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
        self._remember_said(s)
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
        if self.cfg.thinking_sound and not self._speculating():
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

    def _new_voice(self) -> VoiceOut:
        """A mouth for one answer or announcement. Only one ever talks: any other still playing (an announcement when
        they start a new question) is stopped first, with what it had queued."""
        old = [v for v in self._mouths if v.busy]
        for v in old:
            v.cancel()
        if old and self.audio is not None and not getattr(self.audio, "interrupted", False):
            with contextlib.suppress(Exception):
                self.audio.drop_voice()
        voice = VoiceOut(self.svc.tts(), self.audio, on_start=self._sentence_started, now=self._now)
        self._mouths = [v for v in self._mouths if v.busy] + [voice]
        with contextlib.suppress(Exception):
            self.audio.unmute()
        return voice

    def play_sound(self, name: str) -> None:
        """A sound effect ("tada", "uhoh" ...) through the call's own speaker stream."""
        hit = None
        with contextlib.suppress(Exception):
            hit = self.ctx.audio.sound_samples(name)
        if hit is not None and self.audio is not None:
            with contextlib.suppress(Exception):
                self.audio.play_pcm(hit[0], int(hit[1]))

    def announce(self, text: str) -> None:
        """Say ``text`` now in this session (a reminder rang, a game's tap was judged): after what is being said.
        Still unsaid when the session ends: the service says it with the speaker (never lost)."""
        if not text.strip():
            return
        if self._ended.is_set():
            self.svc.speak_later(text)
            return
        self._unsaid.append(text)
        self._spawn(self._say_now(text))

    async def _say_now(self, text: str) -> None:
        while self._answering():
            await asyncio.sleep(0.1)
        with contextlib.suppress(ValueError):
            self._unsaid.remove(text)
        voice = self._voice = self._new_voice()
        self._saying = True
        try:
            for s in _sentences(text):
                voice.say(s)
            voice.close()
            await voice.done()
        finally:
            self._saying = False
        self.svc.history.add("(LittleBot spoke up by itself)", text)
        self._deadline = max(self._deadline, self._now() + max(4.0, float(self.cfg.follow_up_seconds)))

    def _record_cut(self) -> None:
        voice = self._voice
        if voice is not None and voice.heard:
            self.svc.history.add(self._answer_text, " ".join(voice.heard) + " (interrupted)")

    def _cut_answer(self, *, silence: bool = False) -> None:
        """Stop the answer being thought of or said (they cut in, or added to the question). ``silence``: also drop
        what was already handed to the speaker (not after a barge-in: the call audio has done that, and is still
        sending them their first words)."""
        if self._voice is not None:
            self._voice.cancel()
        if silence and self.audio is not None and not getattr(self.audio, "interrupted", False):
            with contextlib.suppress(Exception):
                self.audio.drop_voice()
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        self._answer = None
        self._gate = None

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
            voice = self._voice
            if voice is None or (voice.first_audio_at is None and not voice.heard):
                return  # over the thinking sound, before any answer: their words join the question (_start_answer)
            self.barge_ins += 1
            self._cut_in_at = self._now()
            self._record_cut()
            self._cut_answer()
            self._publish(ev.INTERRUPTED, {})
            log.info("assistant: interrupted; listening to them")
        elif what == "carried_on":  # a false alarm: it talks on
            self._set_phase("speaking" if self._answering() else "listening")
        elif what == "stop_word":
            said = str(info.get("said") or "stop")
            self.barge_ins += 1
            self._cut_in_at = self._now()
            self._record_cut()
            self._cut_answer()
            with contextlib.suppress(Exception):
                self.audio.drop_voice()
            self._publish(ev.INTERRUPTED, {"stop": True})
            if said in PAUSE_WORDS:  # "wait", "hold on": quiet, and listening for what they say next
                self._deadline = self._now() + max(2.0, float(self.cfg.follow_up_seconds))
                self._set_phase("listening")
                log.info("assistant: '%s': quiet now, listening", said)
            else:  # "stop", "be quiet", "enough": quiet, and the conversation is over
                log.info("assistant: '%s': stopped", said)
                self.end(f"they said {said}")

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
                if speaking and mouth[0] >= 0.05:
                    self.mouth_frames += 1  # for the turn's log line: the face's mouth really moved
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
        if self._faces is not None:
            with contextlib.suppress(Exception):
                self._faces.close()
        self._heard_audio.clear()
        self._heard_n = 0
        unsaid, self._unsaid = self._unsaid, []
        for text in unsaid:  # an announcement that came as it ended: the speaker says it now
            self.svc.speak_later(text)
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


SNOOZE = re.compile(r"^\s*(?:(?:okay|ok|please|little ?bot)[\s,]*)*snooze(?: (?:it|that|the alarm))?(?: for \w+ "
                    r"minutes?)?[\s,]*(?:please)?[.!]*\s*$", re.I)
STOP_ALARM = re.compile(r"^\s*(?:(?:okay|ok|please|little ?bot)[\s,]*)*(?:stop|turn off|cancel|dismiss) (?:the |that )?"
                        r"(?:alarm|reminder)[\s,]*(?:please)?[.!]*\s*$|^\s*(?:i'?m up|i'?m awake|got it)[.!]*\s*$", re.I)


class _GatedToolbox:
    """An early answer's toolbox: a tool runs only once their turn really ended (``gate``)."""

    def __init__(self, box: Any, gate: asyncio.Event) -> None:
        self._box = box
        self._gate = gate

    def __getattr__(self, name: str) -> Any:
        return getattr(self._box, name)

    async def run(self, name: str, arguments: Any = None) -> str:
        await self._gate.wait()
        return await self._box.run(name, arguments)


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def _same_words(a: str, b: str) -> bool:
    """The same words, whatever the punctuation and case ("What's the weather?" / "what's the weather")."""
    def norm(t: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9' ]+", " ", t.lower()).split())

    return norm(a) == norm(b)


def _child_name(ctx: Any) -> str | None:
    try:
        from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

        return clean_child_name((ctx.settings.get("profile") or {}).get("nickname"))
    except Exception:  # noqa: BLE001
        return None


def _claim(ctx: Any, voices: Any, text: str) -> Any:
    """They said who they are ("I'm Ali", "it's her dad"): the ``voices.naming.Claim``, else None."""
    try:
        from littlebot.voices.naming import claimed  # noqa: PLC0415

        return claimed(text, voices.store.people.people(), robot_name=ctx.config.robot_name)
    except Exception:  # noqa: BLE001
        return None


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
