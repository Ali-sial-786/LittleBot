"""Who is talking during one live voice call, by voice (littlebot/chatgpt/live.py).

* From the beginning: the wake word keeps what was said with "Hey LittleBot" in memory
  (littlebot/audio/carry.py, ``SpeechCarry.voice_audio``). ``start_from_wake(carry)`` identifies it while
  the call connects; ``intro()`` then gives the call's instructions "The grown-up who woke you sounds like
  Ali (dad), by voice." (or, when it comes too late for them, one quiet note).
* Each of their turns: ``tap(mic)`` wraps the call's microphone and keeps a copy of what it sends in a small
  in-memory buffer (at most ``TURN_MAX_S``); ``turn_ended(text)`` (their final words, from ChatGPT's
  transcript) cuts it there and identifies it. Every identified turn publishes ``Topics.VOICE_SPEAKER``;
  when the grown-up talking is someone the voice wasn't told about yet, ONE quiet note goes through
  ``tell`` ("By voice, the grown-up talking now is Ali (dad). Don't announce this; just know it."). The
  little one's voice is never a note.
* "I'm Ali" (``littlebot.voices.naming``): their voiceprint is learned from that turn when it is one clear
  voice (or from their next turn, when "I'm Ali" alone was too short); a name that isn't on Who's who: the
  voice is told it can be added on the dashboard. When very sure who is talking, the voiceprint improves
  (``VoiceRecognition.improve``).
* "Do you know who I am?": ``answer_now(faces)`` puts the face and the voice evidence together, and says
  honestly when they disagree.

Privacy: the buffer holds only what the call sends anyway (silence while LittleBot talks), is cut at
every turn, cleared when recognising is switched off and when the call ends, and is never written
anywhere. Nothing is buffered or looked at while "Recognise family voices" is off. Only names and roles
go to the voice; with ``ai.share_child_name`` off the child's nickname becomes "the little one".
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from littlebot.voices import naming
from littlebot.voices.service import (
    MIN_SPEECH_S,
    ONE_VOICE_MIN,
    SHORT_MIN_SPEECH_S,
    Heard,
    VoiceMatch,
)

log = logging.getLogger("littlebot.voices")

CALL_RATE = 48000  # the call's microphone frames (int16, 20 ms)
TURN_MAX_S = 20.0  # at most this much of their sound is held for one turn (the newest)
TICK_S = 0.25
WAKE_SEAL_WAIT_S = 3.0  # the wake word hands over what it kept when live voice takes the microphone
NOTE_STALE_S = 30.0  # a note that couldn't be sent in this long is dropped
CLAIM_WAIT_S = 20.0  # "I'm Ali" too short to learn from: their next turn this soon may be used instead
CLAIMS_PER_CALL = 3
LATEST_FRESH_S = 30.0  # "who am I?" may use the last turn's voice this long after it
QUIET = "Don't announce this; just know it."
VOICES_INTRO = ("LittleBot also recognises the family's voices, on the robot, and tells you who is talking when it is "
                "sure. Use only the names LittleBot gives you; never guess who someone is from their voice. When "
                "someone asks who they are, delegate it to the client as \"who is here\" and wait.")
WOKE = "The grown-up who woke you sounds like {who}, by voice."
SPEAKER_NOTE = "By voice, the grown-up talking now is {who}. " + QUIET
STOPPED = "LittleBot has stopped recognising voices, so you don't know who is talking by voice any more. " + QUIET
LEARNED_FIRST = ("LittleBot will now recognise {name}'s voice: it keeps only a voiceprint on the robot, never a "
                 "recording. You may say so in a few words.")
UNKNOWN_NAME = ("Nobody called {name} is on LittleBot's Who's who list yet, so LittleBot can't learn their voice. If it "
                "fits, say briefly that a grown-up can add them under Who's who on the Profile page of the dashboard.")


def _hidden(text: str, hide: str | None) -> str:
    return re.sub(rf"\b{re.escape(hide)}\b", "the little one", text, flags=re.IGNORECASE) if hide else text


class VoiceTap:
    """The call's microphone, with a copy of every frame it sends kept for ``CallVoices`` (only while it's on)."""

    def __init__(self, mic: Any, voices: CallVoices) -> None:
        self._mic = mic
        self._voices = voices

    async def read_frame(self) -> np.ndarray:
        frame = await self._mic.read_frame()
        self._voices.heard(frame)
        return frame

    def __getattr__(self, name: str) -> Any:  # everything else is the microphone's own
        return getattr(self._mic, name)


class CallVoices:
    """Voices during one call. ``tell(text) -> bool``: a quiet note (False: not now, tried again);
    ``publish(data)``: a ``Topics.VOICE_SPEAKER`` event; ``spawn(coro)``: a task of the call's own."""

    def __init__(self, voices: Any, *, tell: Callable[[str], bool], publish: Callable[[dict[str, Any]], None],
                 spawn: Callable[[Any], Any], hide: str | None = None, now: Callable[[], float] = time.monotonic) -> None:
        self.voices = voices
        self.tell = tell
        self.publish = publish
        self.spawn = spawn
        self.hide = hide
        self._now = now
        self.active = self._allowed()
        self._frames: collections.deque[np.ndarray] = collections.deque(maxlen=int(TURN_MAX_S * 50))
        self._lock = asyncio.Lock()  # one turn at a time, in order
        self.checked = asyncio.Event()  # the wake phrase was identified (or can't be): the intro can say who
        self.wake: Heard | None = None
        self.latest: tuple[Heard, float] | None = None  # the last turn's result, and when
        self.told_id: int | None = None  # the grown-up the voice was last told is talking
        self._pending: list[tuple[str, float, int | None]] = []  # notes to send at a quiet moment (text, when, who)
        self._intro_done = False
        self._claim: tuple[int, np.ndarray | None, float] | None = None  # "I'm Ali" too short: (id, voiceprint, until)
        self._claims = 0
        self._named: set[str] = set()
        self.turns = 0
        self.closed = False

    # ------------------------------------------------------------ gates
    def _allowed(self) -> bool:
        try:
            return self.voices.blocked_reason() is None
        except Exception:  # noqa: BLE001
            return False

    def _label(self, m: VoiceMatch) -> str:
        return _hidden(m.label, self.hide)

    # ------------------------------------------------------------ the microphone
    def tap(self, mic: Any) -> VoiceTap:
        return VoiceTap(mic, self)

    def heard(self, frame: np.ndarray) -> None:
        """One frame the call sent (the call's own loop): kept only while recognising is on."""
        if self.active and not self.closed:
            self._frames.append(np.array(frame, dtype=np.int16, copy=True))

    # ------------------------------------------------------------ from the beginning: the wake phrase
    def start_from_wake(self, carry: Any) -> None:
        """Identify who said "Hey LittleBot" (``carry``: the wake word's SpeechCarry), in the background."""
        if carry is None or not self.active or not callable(getattr(carry, "voice_audio", None)):
            self.checked.set()
            return
        self.spawn(self._wake_check(carry))

    async def _wake_check(self, carry: Any) -> None:
        t0 = self._now()
        try:
            deadline = time.monotonic() + WAKE_SEAL_WAIT_S
            while getattr(carry, "is_open", False) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)  # it is complete once live voice takes the microphone
            audio = carry.voice_audio()
            forget = getattr(carry, "forget_voice", None)
            if callable(forget):
                forget()  # the call's own copy, below, is the only one left
            if not len(audio):
                return
            heard = await self.voices.analyse(audio, int(carry.sample_rate), take="first")
            del audio
            self.wake = heard
            if heard.reason != "nobody":  # nobody has recorded a voice: nothing to tell (as for turns)
                self._publish(heard, "wake")
            m = heard.match
            log.info("live voice: the wake phrase was %s by voice in %d ms (%.1f s of speech)",
                     "recognised" if m else f"not recognised ({heard.reason})", int((self._now() - t0) * 1000),
                     heard.speech_s)
            if m is not None and not m.is_child and self._intro_done and self.told_id != m.person_id:
                self._queue(SPEAKER_NOTE.format(who=self._label(m)), m.person_id)  # too late for the instructions
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - knowing who woke it must never break the call
            log.warning("live voice: recognising the wake phrase's voice failed", exc_info=True)
        finally:
            self.checked.set()

    def intro(self) -> str:
        """For the call's instructions (built just before connecting)."""
        self._intro_done = True
        if not self.active or not self.voices.store.has_samples():
            return ""
        text = VOICES_INTRO
        m = self.wake.match if self.wake is not None else None
        if m is not None and not m.is_child:
            text += " " + WOKE.format(who=self._label(m))
            self.told_id = m.person_id
            log.info("live voice: the instructions say who woke it, by voice")
        return text

    # ------------------------------------------------------------ their turns
    def turn_ended(self, text: str) -> None:
        """Their final words of a turn (ChatGPT's transcript): what they said since the last turn is identified."""
        if self.closed:
            return
        frames = list(self._frames)
        self._frames.clear()
        if not self.active or not text.strip():
            return
        self.turns += 1
        self.spawn(self._turn(frames, text, self.turns))

    async def _turn(self, frames: list[np.ndarray], text: str, turn: int) -> None:
        async with self._lock:
            try:
                await self._identify_turn(frames, text, turn)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - must never break the call
                log.warning("live voice: recognising a turn's voice failed", exc_info=True)

    async def _identify_turn(self, frames: list[np.ndarray], text: str, turn: int) -> None:
        pcm = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
        del frames
        people = self.voices.store.people.people()
        claim = naming.claimed(text, people)
        heard = await self.voices.analyse(pcm, CALL_RATE, min_speech_s=SHORT_MIN_SPEECH_S, take="last")
        if heard.reason not in ("ok", "unsure", "too_short", "nobody"):
            return  # switched off or no model meanwhile: nothing looked at
        if heard.match is not None and heard.speech_s < MIN_SPEECH_S:
            heard = Heard("unsure", heard.speech_s, None, heard.best_id, heard.best, heard.second, None,
                          heard.voiceprint)  # too short to name someone (still good for "I'm Ali", below)
        learning = claim is not None or self._claim is not None or self._sure(heard.match)
        if learning and heard.voiceprint is not None:
            heard = await self._with_one_voice(heard, pcm)
        del pcm
        now = self._now()
        if heard.reason != "nobody":
            self.latest = (heard, now)
            self._publish(heard, "turn")
        m = heard.match
        if m is not None and not m.is_child and m.person_id != self.told_id \
                and not any(who == m.person_id for _t, _w, who in self._pending):
            self._pending = [p for p in self._pending if p[2] is None]  # only the newest speaker matters
            self._queue(SPEAKER_NOTE.format(who=self._label(m)), m.person_id)
        log.debug("live voice: turn %d by voice: %s (%.1f s of speech)", turn, heard.reason, heard.speech_s)
        if claim is not None:
            self._claimed(claim, heard, now)
        elif self._claim is not None:
            self._claim_follow_up(heard, now)
        elif m is not None:
            self.voices.improve(heard)

    @staticmethod
    def _sure(m: VoiceMatch | None) -> bool:
        from littlebot.voices.service import LEARN_MARGIN, LEARN_SCORE  # noqa: PLC0415

        return m is not None and m.score >= LEARN_SCORE and m.margin >= LEARN_MARGIN

    async def _with_one_voice(self, heard: Heard, pcm: np.ndarray) -> Heard:
        """``heard`` with ``one_voice`` checked (two more voiceprints; only when it may be learned from)."""
        try:
            again = await self.voices.analyse(pcm, CALL_RATE, min_speech_s=SHORT_MIN_SPEECH_S, take="last",
                                              one_voice=True)
        except Exception:  # noqa: BLE001
            return heard
        return Heard(heard.reason, heard.speech_s, heard.match, heard.best_id, heard.best, heard.second,
                     again.one_voice, heard.voiceprint)

    def _claimed(self, claim: naming.Claim, heard: Heard, now: float) -> None:
        """They said who they are ("I'm Ali")."""
        if claim.person is None:
            key = claim.name.lower()
            if key not in self._named:
                self._named.add(key)
                log.info("live voice: someone said a name that isn't on Who's who; the voice was told")
                self._queue(UNKNOWN_NAME.format(name=_hidden(claim.name, self.hide)), None)
            return
        pid = int(claim.person["id"])
        self.told_id = pid  # the voice heard them say it: no "by voice it's Ali" note needed
        self._pending = [p for p in self._pending if p[2] is None]
        if self._claims >= CLAIMS_PER_CALL:
            return
        result = self.voices.keep_claimed(pid, heard)
        if result.get("reason") == "too_short":
            self._claim = (pid, heard.voiceprint, now + CLAIM_WAIT_S)
            log.info("live voice: they said who they are, too briefly to learn their voice; trying their next turn")
            return
        self._after_claim(claim.person, result)

    def _claim_follow_up(self, heard: Heard, now: float) -> None:
        """The turn after a too-short "I'm Ali": learn from it when it is the same, one clear voice."""
        pid, short, until = self._claim  # type: ignore[misc]
        self._claim = None
        if now > until or heard.voiceprint is None or heard.one_voice is not True:
            return
        if short is not None and short.size == heard.voiceprint.size \
                and float(np.dot(short, heard.voiceprint)) < ONE_VOICE_MIN:
            log.info("live voice: the turn after \"I'm ...\" didn't sound like the same voice; nothing learned")
            return
        person = self.voices.store.people.person(pid)
        if person is not None:
            self._after_claim(person, self.voices.keep_claimed(pid, heard))

    def _after_claim(self, person: dict[str, Any], result: dict[str, Any]) -> None:
        if not result.get("ok"):
            log.info("live voice: they said who they are; their voice wasn't learned (%s)", result.get("reason"))
            return
        self._claims += 1
        if result.get("first"):
            self._queue(LEARNED_FIRST.format(name=_hidden(person["name"], self.hide)), None)

    # ------------------------------------------------------------ "do you know who I am?"
    async def answer_now(self, faces: Any = None) -> str:
        """Face and voice together, for the voice to say now. ``faces``: the call's CallFaces (or None)."""
        face_text, sighting = None, None
        if faces is not None:
            try:
                if faces.faces.blocked_reason() is None:
                    face_text = await faces.answer_now()
                    sighting = faces.latest
            except Exception:  # noqa: BLE001
                log.debug("live voice: face check for \"who am I?\" failed", exc_info=True)
        heard = await self._now_heard() if self.active else None
        child = None
        try:
            child = self.voices._child()  # noqa: SLF001 - the same nickname the matches use
        except Exception:  # noqa: BLE001
            pass
        text = who_answer(face_text, sighting, heard, hide=self.hide, child=child)
        log.info("live voice: asked who is there (by voice: %s)", heard.reason if heard is not None else "off")
        return text

    async def _now_heard(self) -> Heard | None:
        """Who is talking: what they are saying right now when that names someone, else their last turn (if
        recent), else what they are saying now. What was heard since their last turn often has the end of
        LittleBot's own voice or a cut-in in it, so an "unsure" there doesn't outweigh a turn just recognised."""
        now_heard = None
        frames = list(self._frames)
        if frames:
            pcm = np.concatenate(frames)
            heard = await self.voices.analyse(pcm, CALL_RATE, take="last")
            del pcm
            if heard.match is not None:
                return heard
            if heard.reason != "too_short":
                now_heard = heard
        async with self._lock:  # a turn that just ended is identified first
            latest = self.latest
        fresh = latest[0] if latest is not None and self._now() - latest[1] <= LATEST_FRESH_S else None
        if fresh is not None and (fresh.match is not None or now_heard is None):
            if now_heard is not None:
                log.info("live voice: \"who am I?\": not sure from just now (%s); their last turn is used",
                         now_heard.reason)
            return fresh
        if now_heard is not None:
            return now_heard
        return Heard("too_short") if self.voices.store.has_samples() else Heard("nobody")

    # ------------------------------------------------------------ notes, events
    def _queue(self, text: str, who: int | None) -> None:
        self._pending.append((text, self._now(), who))
        self._deliver()

    def _deliver(self) -> None:
        now = self._now()
        self._pending = [p for p in self._pending if now - p[1] < NOTE_STALE_S]
        while self._pending:
            text, _at, who = self._pending[0]
            if who is not None and who == self.told_id:
                self._pending.pop(0)
                continue
            if not self.tell(text):
                return
            self._pending.pop(0)
            if who is not None:
                self.told_id = who
                log.info("live voice: told who is talking, by voice")

    def _publish(self, heard: Heard, source: str) -> None:
        m = heard.match
        data = {"person_id": m.person_id if m else None, "name": m.name if m else None, "role": m.role if m else None,
                "is_child": m.is_child if m else None, "score": round(float(heard.best), 3),
                "speech_s": round(float(heard.speech_s), 2), "source": source}
        try:
            self.publish(data)
        except Exception:  # noqa: BLE001
            log.debug("publishing who is talking failed", exc_info=True)

    async def run(self) -> None:
        """Until ``close``: follow the privacy switch, and send waiting notes at quiet moments."""
        while not self.closed:
            allowed = self._allowed()
            if self.active and not allowed:
                self.active = False
                self._frames.clear()
                self.latest, self.wake, self._claim = None, None, None
                self._pending = []
                if self.told_id is not None and self.voices.blocked_reason() == "off":
                    self._pending.append((STOPPED, self._now(), None))
                self.told_id = None
                log.info("live voice: voice recognition stopped (%s)", self.voices.blocked_reason())
            elif allowed and not self.active:
                self.active = True
            self._deliver()
            await asyncio.sleep(TICK_S)

    def close(self) -> None:
        self.closed = True
        self.active = False
        self._frames.clear()
        self._pending = []
        self.latest = self.wake = None
        self._claim = None


# ------------------------------------------------------------------ "do you know who I am?"
def who_answer(face_text: str | None, sighting: Any, heard: Heard | None, *, hide: str | None = None,
               child: str | None = None) -> str:
    """What to tell the voice: the face check's answer (``face_text``, None when faces can't look) and/or the
    voice's (``heard``, None when voice recognition is off). They are compared; a disagreement is said as it is."""
    from littlebot.vision.faces import describe, is_child  # noqa: PLC0415

    m = heard.match if heard is not None else None
    if m is not None and m.is_child:
        m = None  # whoever asks is talking: the little one doesn't ask
    if m is None:
        if face_text is not None:
            if heard is not None and heard.reason in ("unsure", "too_short"):
                return f"{face_text} By voice, LittleBot can't tell who is talking."
            return face_text
        if heard is None:
            return ("LittleBot can't recognise who is there right now, so you can't tell who is talking. Say so "
                    "briefly; never guess.")
        if heard.reason == "nobody":
            return ("Nobody has recorded their voice for LittleBot yet, so you can't tell who is talking. Say so; a "
                    "grown-up can record their voice under Who's who on the Profile page of the dashboard.")
        if heard.reason == "too_short":
            return ("LittleBot didn't hear enough of their voice to tell who it is. Say you're not sure yet and ask "
                    "who they are; never guess a name.")
        return ("LittleBot doesn't recognise this voice. Say you don't recognise them yet, and never guess a name; a "
                "grown-up can add them under Who's who on the dashboard.")
    who = _hidden(m.label, hide)
    if face_text is None or sighting is None:
        return f"By voice, LittleBot recognises the person talking as {who}. Answer warmly, by name."
    people = list(sighting.people())
    grown = [p for p in people if not is_child(p.name, p.role, child)]
    seen = describe(people, hide)
    if any(p.id == m.person_id for p in grown):
        if len(grown) == 1:
            return f"{face_text} Their voice sounds like {who} too."
        return (f"LittleBot's face recognition sees {seen} in front of the camera, and by voice the one talking is "
                f"{who}. Answer warmly, by name.")
    if grown:
        return (f"LittleBot's face recognition sees {seen} in front of the camera, but by voice the person talking "
                f"sounds like {who}. They don't agree: say honestly what each one says, and ask who is talking; "
                "never guess.")
    return (f"LittleBot's face recognition doesn't see {_hidden(m.name, hide)} in front of the camera, but by voice the "
            f"person talking sounds like {who}. Answer by name, and say you know their voice.")


__all__ = ["CallVoices", "VoiceTap", "who_answer"]
