"""WhoTracker: who is talking across a conversation, voice and face together."""

from __future__ import annotations

from dataclasses import dataclass

from littlebot.assistant.who import STICKY_S, WhoTracker
from littlebot.voices.service import Heard, VoiceMatch


@dataclass
class Person:
    id: int
    name: str
    role: str


ALI, SARA, AMARA = Person(1, "Ali", "dad"), Person(2, "Sara", "mum"), Person(3, "Amara", "baby")
NAMES = {1: ("Ali", "dad"), 2: ("Sara", "mum"), 3: ("Amara", "baby")}


def sure(p: Person, score: float = 0.7) -> Heard:
    return Heard("ok", 2.0, VoiceMatch(p.id, p.name, p.role, score, p.role == "baby", 0.3), p.id, score, 0.3)


def unsure(best_id: int, best: float, second: float = 0.3) -> Heard:
    return Heard("unsure", 1.5, None, best_id, best, second)


def tracker(clock: list[float]) -> WhoTracker:
    return WhoTracker(threshold=0.5, is_child=lambda n, r: r == "baby", lookup=NAMES.get, now=lambda: clock[0])


def test_sure_then_consistent_unclear_turns_keep_them() -> None:
    t = [0.0]
    w = tracker(t)
    assert w.voice(sure(ALI)).label == "Ali (dad)"
    t[0] = 10
    who = w.voice(unsure(1, 0.42))  # not clear alone, but closest to Ali and not far off
    assert who.person_id == 1 and who.sure
    who = w.voice(Heard("too_short", 0.3))  # "yes": nothing to go on
    assert who.person_id == 1 and who.sure


def test_clearly_someone_else_switches_and_doubt_unsures() -> None:
    t = [0.0]
    w = tracker(t)
    w.voice(sure(ALI))
    who = w.voice(unsure(2, 0.56))  # above the threshold but too close to call, closest to Sara
    assert who.person_id == 1 and not who.sure
    who = w.voice(sure(SARA))
    assert who.person_id == 2 and who.sure and who.how == "voice"


def test_voice_and_face_together() -> None:
    t = [0.0]
    w = tracker(t)
    w.faces([SARA, AMARA])  # the baby never counts
    who = w.voice(unsure(2, 0.38))  # the voice alone isn't enough, but Sara is the one grown-up in view
    assert who.person_id == 2 and who.sure and who.how == "voice and face"
    w2 = tracker(t)
    w2.faces([SARA])
    assert w2.current().how == "face" and not w2.current().sure  # no voice yet: probably, by face


def test_the_baby_is_never_who_is_talking_and_it_fades() -> None:
    t = [0.0]
    w = tracker(t)
    assert w.voice(sure(AMARA)) is None
    w.voice(sure(ALI))
    t[0] = STICKY_S + 1
    assert not w.current().sure  # long ago: probably only
    t[0] = 10_000
    assert w.current() is None


def test_they_say_who_they_are() -> None:
    w = tracker([0.0])
    who = w.said(2, "Sara", "mum")
    assert who.sure and "said" in who.how
    assert "probably" not in who.note()
