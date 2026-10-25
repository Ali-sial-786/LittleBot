"""Who is talking during a live voice call, by voice (littlebot/voices/call.py), with the fake model.

``CallVoices`` is driven directly here (no WebRTC): frames go in as the call's microphone would send them,
turns end as ChatGPT's transcript would end them, and the notes and events it makes are collected.
tests/chatgpt/test_live_voices.py runs the same through a real local call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from voice_helpers import add_person, enrol, say

from littlebot.audio.carry import VOICE_KEEP_SECONDS, PreRoll, SpeechCarry
from littlebot.testing import FakeSpeakerEngine, speaker_tone
from littlebot.voices import call as C
from littlebot.voices.service import Heard, VoiceMatch, VoiceRecognition


class Call:
    """What the live call gives CallVoices: quiet notes (when ``quiet``), events, its own tasks."""

    def __init__(self, voices: Any, *, hide: str | None = None) -> None:
        self.notes: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.tasks: list[asyncio.Task] = []
        self.quiet = True
        self.cv = C.CallVoices(voices, tell=self.tell, publish=self.events.append, spawn=self.spawn, hide=hide)

    def tell(self, text: str) -> bool:
        if self.quiet:
            self.notes.append(text)
        return self.quiet

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task

    async def settle(self) -> None:
        await asyncio.gather(*[t for t in self.tasks if not t.done()])

    def speak(self, pcm: np.ndarray) -> None:
        """``pcm`` (48 kHz int16) through the call's microphone, 20 ms at a time."""
        for i in range(0, len(pcm), 960):
            self.cv.heard(pcm[i:i + 960])

    async def turn(self, pcm: np.ndarray, text: str = "Can we read a book?") -> dict[str, Any] | None:
        before = len(self.events)
        self.speak(pcm)
        self.cv.turn_ended(text)
        await self.settle()
        return self.events[-1] if len(self.events) > before else None

    def speaker_notes(self) -> list[str]:
        return [n for n in self.notes if n.startswith("By voice, the grown-up talking now")]


@pytest.fixture
async def family(voices, vctx):
    """Ali (dad, speaker 0) and Sara (mum, speaker 1) recorded their voices; Amara (speaker 2) is the baby."""
    vctx.settings.set("profile", {"nickname": "Amara"})
    ids = SimpleNamespace(ali=add_person(voices, "Ali", "dad"), sara=add_person(voices, "Sara", "mum"),
                          amara=add_person(voices, "Amara", "the baby"))
    for pid, k in ((ids.ali, 0), (ids.sara, 1), (ids.amara, 2)):
        assert (await enrol(voices, pid, k))["ok"]
    return ids


def wake_carry(speaker: int | None, *, phrase_s: float = 1.0, after_s: float = 1.0, keep_voice: bool = True) -> SpeechCarry:
    """What the wake word hands over: "Hey LittleBot" (kept for the voice only) and what followed."""
    c = SpeechCarry(16000, quiet_level=0.0105, keep_voice=keep_voice)
    if speaker is not None:
        c.keep_wake_phrase([speaker_tone(speaker, phrase_s)])
        c.add(speaker_tone(speaker, after_s))
    c.seal("live voice took the microphone")
    return c


# ------------------------------------------------------------------ from the beginning: who said "Hey LittleBot"
async def test_who_woke_it_goes_into_the_instructions(voices, family):
    call = Call(voices)
    carry = wake_carry(0)
    call.cv.start_from_wake(carry)
    await asyncio.wait_for(call.cv.checked.wait(), 2)
    intro = call.cv.intro()
    assert C.VOICES_INTRO in intro and "The grown-up who woke you sounds like Ali (dad), by voice." in intro
    assert call.events == [{"person_id": family.ali, "name": "Ali", "role": "dad", "is_child": False, "score": 1.0,
                            "speech_s": pytest.approx(2.0, abs=0.1), "source": "wake"}]
    assert len(carry.voice_audio()) == 0  # the wake word's copy is gone once looked at
    # Ali goes on talking: the voice already knows, no note
    await call.turn(say(0, 2.0))
    assert call.speaker_notes() == []


async def test_who_woke_it_when_it_was_the_little_one_or_nobody_known(voices, family):
    call = Call(voices)
    call.cv.start_from_wake(wake_carry(2))  # Amara's babbling woke it
    await asyncio.wait_for(call.cv.checked.wait(), 2)
    intro = call.cv.intro()
    assert intro == C.VOICES_INTRO and call.events[0]["is_child"] is True
    stranger = Call(voices)
    stranger.cv.start_from_wake(wake_carry(4))
    await asyncio.wait_for(stranger.cv.checked.wait(), 2)
    assert stranger.cv.intro() == C.VOICES_INTRO
    assert stranger.events[0]["person_id"] is None and stranger.events[0]["is_child"] is None


async def test_without_voiceprints_the_instructions_say_nothing_about_voices(voices):
    call = Call(voices)
    call.cv.start_from_wake(wake_carry(0))
    await asyncio.wait_for(call.cv.checked.wait(), 2)
    assert call.cv.intro() == "" and call.events == []  # "nobody" is not published


async def test_who_woke_it_known_too_late_for_the_instructions_is_one_quiet_note(voices, family):
    call = Call(voices)
    assert call.cv.intro() == C.VOICES_INTRO  # connecting didn't wait long enough
    call.cv.start_from_wake(wake_carry(1))
    await call.settle()
    assert call.speaker_notes() == ["By voice, the grown-up talking now is Sara (mum). Don't announce this; "
                                    "just know it."]


async def test_a_carry_without_the_voice_copy_is_fine(voices, family):
    call = Call(voices)
    call.cv.start_from_wake(wake_carry(0, keep_voice=False))
    await asyncio.wait_for(call.cv.checked.wait(), 2)
    assert call.cv.wake is None and call.events == [] and "woke you" not in call.cv.intro()
    nothing = Call(voices)
    nothing.cv.start_from_wake(None)  # started from the dashboard
    assert nothing.cv.checked.is_set()


# ------------------------------------------------------------------ their turns
async def test_each_turn_is_published_and_the_voice_is_told_once_per_new_speaker(voices, family):
    call = Call(voices)
    call.cv.intro()
    ev = await call.turn(say(0, 2.0))
    assert ev == {"person_id": family.ali, "name": "Ali", "role": "dad", "is_child": False, "score": 1.0,
                  "speech_s": pytest.approx(2.0, abs=0.1), "source": "turn"}
    assert call.speaker_notes() == ["By voice, the grown-up talking now is Ali (dad). Don't announce this; just "
                                    "know it."]
    await call.turn(say(0, 1.5))
    await call.turn(say(0, 3.0))
    assert len(call.events) == 3 and len(call.speaker_notes()) == 1  # every turn published, told once
    await call.turn(say(1, 2.0))
    assert call.events[-1]["name"] == "Sara" and call.speaker_notes()[-1].startswith(
        "By voice, the grown-up talking now is Sara (mum).")
    await call.turn(say(0, 2.0))  # Ali again: a change, so told again
    assert len(call.speaker_notes()) == 3 and "Ali (dad)" in call.speaker_notes()[-1]


async def test_the_little_one_is_published_but_never_a_note(voices, family):
    call = Call(voices)
    ev = await call.turn(say(2, 2.0), "ba ba ba")
    assert ev["is_child"] is True and ev["name"] == "Amara" and call.notes == []


async def test_unsure_and_too_short_turns_publish_nobody_and_say_nothing(voices, family):
    call = Call(voices)
    ev = await call.turn(say(4, 2.0))  # a visitor
    assert ev["person_id"] is None and ev["name"] is None and ev["role"] is None and ev["is_child"] is None
    ev = await call.turn(say(0, 0.7), "Yes")  # too little to name anyone
    assert ev["person_id"] is None and ev["is_child"] is None
    assert call.notes == [] and call.cv.told_id is None


async def test_a_turn_holds_only_its_own_sound_and_at_most_the_newest_seconds(voices, family):
    call = Call(voices)
    call.speak(say(0, 2.0))
    call.cv.turn_ended("Hello")
    call.speak(say(1, 2.0))  # the next turn starts as the last one is still being identified
    await call.settle()
    assert call.events[-1]["name"] == "Ali"
    await call.turn(np.zeros(0, np.int16), "And Sara now")
    assert call.events[-1]["name"] == "Sara"
    call.speak(say(1, C.TURN_MAX_S + 5))
    assert len(call.cv._frames) == int(C.TURN_MAX_S * 50)


async def test_a_note_waits_for_a_quiet_moment_and_goes_stale(voices, family, monkeypatch):
    call = Call(voices)
    call.quiet = False  # someone is talking / a hand-off is under way
    await call.turn(say(0, 2.0))
    assert call.notes == [] and call.cv._pending
    call.quiet = True
    call.cv._deliver()
    assert len(call.speaker_notes()) == 1
    call.quiet = False
    await call.turn(say(1, 2.0))
    monkeypatch.setattr(C, "NOTE_STALE_S", 0.0)
    call.quiet = True
    call.cv._deliver()
    assert len(call.speaker_notes()) == 1  # too old to matter: dropped


async def test_the_tap_passes_the_microphone_through_and_keeps_a_copy(voices, family):
    call = Call(voices)

    class Mic:
        sample_rate = 48000

        def __init__(self) -> None:
            self.frames = [say(0, 0.02) for _ in range(100)]

        async def read_frame(self) -> np.ndarray:
            return self.frames.pop()

    mic = Mic()
    tap = call.cv.tap(mic)
    assert tap.sample_rate == 48000
    frame = await tap.read_frame()
    frame[:] = 0  # the call's own frame: the copy is separate
    assert len(call.cv._frames) == 1 and np.abs(call.cv._frames[0]).max() > 1000


# ------------------------------------------------------------------ "I'm Ali"
async def test_im_ali_learns_their_voice(voices, vctx):
    ali = add_person(voices, "Ali", "dad")
    call = Call(voices)
    await call.turn(say(0, 3.0), "Hi LittleBot, it's Ali.")
    assert voices.store.counts()[ali]["learned"] == 1
    assert call.notes == [C.LEARNED_FIRST.format(name="Ali")]
    assert call.cv.told_id == ali  # they said so themselves: no "by voice" note
    await call.turn(say(0, 2.0))
    assert call.speaker_notes() == [] and call.events[-1]["name"] == "Ali"


async def test_im_ali_said_briefly_learns_from_their_next_turn(voices):
    ali = add_person(voices, "Ali", "dad")
    call = Call(voices)
    await call.turn(say(0, 0.7), "I'm Ali")
    assert voices.store.count(ali) == 0 and call.cv._claim is not None
    await call.turn(say(0, 3.0), "What shall we play?")
    assert voices.store.counts()[ali]["learned"] == 1 and call.cv._claim is None


async def test_im_ali_then_someone_else_talking_learns_nothing(voices):
    ali = add_person(voices, "Ali", "dad")
    call = Call(voices)
    await call.turn(say(0, 0.7), "I'm Ali")
    await call.turn(say(3, 3.0), "Hello robot")  # a different voice answered
    assert voices.store.count(ali) == 0


async def test_im_ali_from_sara_is_not_learned(voices, family):
    call = Call(voices)
    await call.turn(say(1, 3.0), "I'm Ali")  # Sara, joking
    assert voices.store.counts()[family.ali]["learned"] == 0


async def test_a_name_not_on_whos_who_tells_the_voice_it_can_be_added(voices, family, vctx):
    vctx.settings.set("ai.share_child_name", False)
    call = Call(voices, hide="Amara")
    await call.turn(say(4, 3.0), "Hello, I'm Sam.")
    await call.turn(say(4, 3.0), "It's Sam speaking")
    assert call.notes == [C.UNKNOWN_NAME.format(name="Sam")]  # once
    assert "Who's who" in call.notes[0] and "dashboard" in call.notes[0]
    assert voices.store.people.people() and all(p["name"] != "Sam" for p in voices.store.people.people())


async def test_claims_per_call_are_capped(voices, monkeypatch):
    ali = add_person(voices, "Ali", "dad")
    monkeypatch.setattr(C, "CLAIMS_PER_CALL", 2)
    call = Call(voices)
    for _ in range(4):
        await call.turn(say(0, 3.0), "It's Ali")
    assert voices.store.counts()[ali]["learned"] == 2


async def test_being_very_sure_in_a_call_improves_the_voiceprint(voices, family):
    call = Call(voices)
    await call.turn(say(0, 3.0))
    assert voices.store.counts()[family.ali]["learned"] == 1
    await call.turn(say(2, 3.0), "ba ba")  # never from the little one
    assert voices.store.counts()[family.amara]["learned"] == 0


async def test_the_childs_name_stays_on_the_robot(voices, vctx):
    vctx.settings.set("profile", {"nickname": "Amara"})
    amara = add_person(voices, "Amara", "")
    gran = add_person(voices, "Nadia", "Amara's grandma")
    await enrol(voices, amara, 2)
    await enrol(voices, gran, 3)
    call = Call(voices, hide="Amara")
    await call.turn(say(3, 2.0))
    assert call.speaker_notes() == ["By voice, the grown-up talking now is Nadia (the little one's grandma). "
                                    "Don't announce this; just know it."]


# ------------------------------------------------------------------ switched off: nothing processed at all
async def test_switched_off_before_the_call_nothing_is_kept_or_looked_at(vctx):
    engine = FakeSpeakerEngine()
    voices = VoiceRecognition(vctx, engine=engine)
    ali = add_person(voices, "Ali", "dad")
    await enrol(voices, ali, 0)
    calls = engine.calls
    vctx.privacy.set(voice_recognition_enabled=False)
    call = Call(voices)
    assert not call.cv.active
    call.cv.start_from_wake(wake_carry(0))
    assert call.cv.checked.is_set()
    call.speak(say(0, 2.0))
    assert len(call.cv._frames) == 0  # not even buffered
    call.cv.turn_ended("It's Ali")
    await call.settle()
    assert call.cv.intro() == "" and engine.calls == calls and call.events == [] and call.notes == []


async def test_switched_off_during_the_call_forgets_and_stops(voices, family, monkeypatch):
    monkeypatch.setattr(C, "TICK_S", 0.01)
    call = Call(voices)
    runner = asyncio.ensure_future(call.cv.run())  # (not one of the turns settle() waits for)
    await call.turn(say(0, 2.0))
    assert len(call.speaker_notes()) == 1
    call.speak(say(0, 1.0))
    voices.ctx.privacy.set(voice_recognition_enabled=False)
    for _ in range(100):
        if not call.cv.active:
            break
        await asyncio.sleep(0.01)
    assert not call.cv.active and len(call.cv._frames) == 0 and call.cv.latest is None
    await asyncio.sleep(0.05)
    assert call.notes[-1] == C.STOPPED  # it had said who was talking: now it says it doesn't know any more
    events = len(call.events)
    await call.turn(say(0, 2.0))
    assert len(call.events) == events and len(call.cv._frames) == 0
    call.cv.close()
    await asyncio.wait_for(runner, 1)


async def test_closing_forgets_everything(voices, family):
    call = Call(voices)
    call.speak(say(0, 1.0))
    await call.turn(say(0, 2.0))
    call.speak(say(0, 1.0))
    call.cv.close()
    assert len(call.cv._frames) == 0 and call.cv.latest is None and not call.cv.active
    call.cv.heard(say(0, 0.02))
    call.cv.turn_ended("more")
    assert len(call.cv._frames) == 0 and call.cv.turns == 1


async def test_logs_never_hold_names(voices, family, caplog):
    caplog.set_level(logging.DEBUG, logger="littlebot.voices")
    call = Call(voices)
    call.cv.start_from_wake(wake_carry(0))
    await call.settle()
    await call.turn(say(1, 2.0), "I'm Sara")
    await call.turn(say(4, 2.0), "I'm Sam")
    assert "Ali" not in caplog.text and "Sara" not in caplog.text and "Sam" not in caplog.text


# ------------------------------------------------------------------ "do you know who I am?"
def match(pid: int, name: str, role: str = "", *, child: bool = False) -> VoiceMatch:
    return VoiceMatch(pid, name, role, 0.8, child, 0.5)


def sighting(*people: tuple[int, str, str]) -> Any:
    from littlebot.vision import faces

    return faces.Sighting(0.0, tuple(faces.Seen(faces.Known(pid, name, role, np.ones(128, np.float32)), 0.8,
                                                (0.1, 0.3, 0.2, 0.3)) for pid, name, role in people))


def test_who_answer_by_voice_alone():
    ok = Heard("ok", 2.0, match(1, "Ali", "dad"))
    assert C.who_answer(None, None, ok) == "By voice, LittleBot recognises the person talking as Ali (dad). " \
                                           "Answer warmly, by name."
    assert "doesn't recognise this voice" in C.who_answer(None, None, Heard("unsure", 2.0))
    assert "didn't hear enough" in C.who_answer(None, None, Heard("too_short", 0.3))
    assert "Nobody has recorded their voice" in C.who_answer(None, None, Heard("nobody"))
    assert "can't recognise who is there" in C.who_answer(None, None, None)
    child = Heard("ok", 2.0, match(3, "Amara", "the baby", child=True))
    assert "doesn't recognise this voice" in C.who_answer(None, None, child)  # the little one doesn't ask
    assert "Amara" not in C.who_answer(None, None, Heard("ok", 2.0, match(1, "Amara's dad", "")), hide="Amara")


def test_who_answer_face_and_voice_agree_or_disagree():
    face = "The grown-up talking to you is Ali (dad)."
    ali, sara = Heard("ok", 2.0, match(1, "Ali", "dad")), Heard("ok", 2.0, match(2, "Sara", "mum"))
    assert C.who_answer(face, sighting((1, "Ali", "dad")), ali) == face + " Their voice sounds like Ali (dad) too."
    both = C.who_answer("Ali and Sara are here.", sighting((1, "Ali", "dad"), (2, "Sara", "mum")), sara)
    assert "by voice the one talking is Sara (mum)" in both
    disagree = C.who_answer(face, sighting((1, "Ali", "dad")), sara)
    assert "sounds like Sara (mum)" in disagree and "They don't agree" in disagree and "never guess" in disagree
    away = C.who_answer("Nobody is in view.", sighting(), ali)
    assert "doesn't see Ali in front of the camera" in away and "know their voice" in away
    assert C.who_answer(face, sighting((1, "Ali", "dad")), Heard("unsure", 2.0)) == \
        face + " By voice, LittleBot can't tell who is talking."
    assert C.who_answer(face, sighting((1, "Ali", "dad")), None) == face  # voices off: the face answer alone


async def test_answer_now_uses_what_they_are_saying_or_their_last_turn(voices, family):
    call = Call(voices)
    call.speak(say(1, 2.0))  # Sara asking, right now
    text = await call.cv.answer_now(None)
    assert "Sara (mum)" in text
    call.cv._frames.clear()
    await call.turn(say(0, 2.0))
    assert "Ali (dad)" in await call.cv.answer_now(None)  # the turn that just ended


async def test_answer_now_unsure_just_now_falls_back_to_their_last_turn(voices, family, caplog):
    call = Call(voices)
    await call.turn(say(0, 2.0))  # Ali, recognised
    call.speak(say(7, 2.0))  # since then: a voice nobody recorded (in a real call: LittleBot's own echo, a cut-in)
    with caplog.at_level(logging.INFO, logger="littlebot.voices"):
        text = await call.cv.answer_now(None)
    assert "Ali (dad)" in text and "their last turn is used" in caplog.text
    call.cv.latest = (call.cv.latest[0], time.monotonic() - C.LATEST_FRESH_S - 1)  # too long ago: just now counts
    assert "doesn't recognise this voice" in await call.cv.answer_now(None)


async def test_answer_now_combines_with_faces(voices, family):
    class Faces:
        def __init__(self) -> None:
            self.faces = SimpleNamespace(blocked_reason=lambda: None)
            self.latest = sighting((1, "Ali", "dad"))

        async def answer_now(self) -> str:
            return "The grown-up talking to you is Ali (dad)."

    call = Call(voices)
    call.speak(say(1, 2.0))
    text = await call.cv.answer_now(Faces())
    assert "They don't agree" in text and "Sara (mum)" in text


# ------------------------------------------------------------------ the wake word's copy of the wake phrase
def test_the_carry_keeps_a_voice_copy_only_when_asked_and_drops_it():
    plain = SpeechCarry(16000)
    plain.keep_wake_phrase([np.ones(1600, np.float32)])
    plain.add(np.ones(1600, np.float32))
    assert len(plain.voice_audio()) == 0
    c = SpeechCarry(16000, keep_voice=True)
    c.keep_wake_phrase([np.full(800, 0.5, np.float32), np.full(800, 0.5, np.float32)])
    for _ in range(100):  # 10 s after the wake phrase: only the first VOICE_KEEP_SECONDS are kept
        c.add(np.full(1600, 0.1, np.float32))
    v = c.voice_audio()
    assert len(v) == 1600 + int(VOICE_KEEP_SECONDS * 16000) and v[0] == 0.5
    assert len(c.take()) == 160000  # the call's own carry is untouched by it
    assert len(c.voice_audio()) == len(v)  # taking the carry leaves the voice copy (it's looked at separately)
    c.forget_voice()
    assert len(c.voice_audio()) == 0
    d = SpeechCarry(16000, keep_voice=True)
    d.add(np.ones(1600, np.float32))
    d.drop("live voice could not start")
    assert len(d.voice_audio()) == 0
    d.keep_wake_phrase([np.ones(10, np.float32)])
    assert len(d.voice_audio()) == 0


def test_preroll_before_gives_the_wake_phrase():
    pre = PreRoll(10, max_seconds=5.0)
    for i in range(10):
        pre.add(i * 2, np.full(2, i, np.float32))  # samples 0..19
    assert [list(b) for b in pre.before(5)] == [[0, 0], [1, 1], [2]]
    assert pre.before(None) == [] and pre.before(0) == []


@pytest.mark.parametrize("on", [True, False])
def test_the_wake_word_keeps_the_wake_phrase_only_when_voices_are_on(vctx, on):
    from littlebot.audio.wakeword import WakeWordService

    vctx.privacy.set(voice_recognition_enabled=on)
    wake = WakeWordService(vctx, recognizer_factory=lambda _rate: object())
    pre = PreRoll(16000, 8.0)
    pre.add(0, speaker_tone(0, 2.0))  # "hey little bot" (0-1.2 s) "what's the weather"
    result = {"text": "hey little bot what", "result": [
        {"word": "hey", "start": 0.1, "end": 0.4}, {"word": "little", "start": 0.4, "end": 0.8},
        {"word": "bot", "start": 0.8, "end": 1.2}, {"word": "what", "start": 1.3, "end": 1.6}]}
    carry = wake._carry_from(pre, result, "hey little bot", 16000)
    assert carry.seconds == pytest.approx(0.83, abs=0.01)  # the call gets what followed, as before
    assert carry.keep_voice is on
    assert len(carry.voice_audio()) == (32000 if on else 0)  # the wake phrase too, for the voice only


async def test_identify_takes_about_as_long_as_the_model(voices, family):
    """The call's path adds little to the model's own time (the fake model is instant)."""
    t0 = time.perf_counter()
    for _ in range(20):
        await voices.identify(say(0, 1.5), 48000)
    assert (time.perf_counter() - t0) / 20 < 0.05
