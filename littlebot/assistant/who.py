"""Who is talking, across a whole conversation: voice and face recognition put together (both opt-in, on the robot).

Each recogniser alone judges one moment: a turn of speech (littlebot/voices) or one photo (littlebot/vision/faces).
Alone they are cautious, rightly: a 1-second "yes, that one" is too short to be sure of, a voice that sounds a
bit like two people is nobody. Over a conversation there is more to go on, and :class:`WhoTracker` uses it:

* **sure by voice**: a turn whose voice clearly matches someone (``Heard.match``) sets who is talking;
* **consistent**: a later turn that isn't clear on its own but is closest to the same person, and not far off
  (``KEEP_BELOW`` under the threshold), keeps them, and so does a turn too short to tell (``too_short``): people
  rarely swap mid-conversation; after ``STICKY_S`` without a confirming turn, it is "probably" only;
* **switch**: a turn that clearly matches someone else switches at once; a turn clearly closest to someone else
  (above the threshold but too close to call) drops to "not sure";
* **voice and face**: a voice that is closest to the one grown-up in front of the camera, and not far off
  (``FACE_HELP_BELOW``), is them ("by voice and face");
* **face only**: no voice to go on (voice recognition off, or nothing clear yet) and exactly one grown-up in view:
  "probably" them, by face.

The little one is never "who is talking" (the baby doesn't talk in words). Only names and roles leave this module.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable, Sequence

KEEP_BELOW = 0.12  # an unclear turn closest to the current speaker keeps them if it is at most this under the threshold
FACE_HELP_BELOW = 0.15  # a voice closest to the one grown-up in view is them if at most this under the threshold
SWITCH_DOUBT = 0.04  # an unclear turn this much above the threshold, closest to someone else: no longer sure
STICKY_S = 90.0  # sure this long after the last turn that confirmed it; "probably" after that
FORGET_S = 600.0  # nothing about who is talking for this long: forgotten


@dataclasses.dataclass(frozen=True)
class Who:
    person_id: int
    name: str
    role: str
    how: str  # "voice" | "voice and face" | "face"
    sure: bool

    @property
    def label(self) -> str:
        return f"{self.name} ({self.role})" if self.role else self.name

    def note(self) -> str:
        """For the brain's context."""
        if self.sure:
            return (f"the person talking is {self.label} (recognised by {self.how}; use their name now and then, "
                    "and their own memories)")
        return (f"the person talking is probably {self.label} (by {self.how}, not certain: don't insist on it, "
                "and never say it unprompted)")


class WhoTracker:
    """One per voice session. Feed it each turn's ``voices.service.Heard`` and the faces in view."""

    def __init__(self, *, threshold: float, is_child: Callable[[str, str], bool] = lambda _n, _r: False,
                 lookup: Callable[[int], tuple[str, str] | None] | None = None,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.threshold = float(threshold)
        self._is_child = is_child
        self._lookup = lookup  # person_id -> (name, role) from Who's who (an unclear voice has only an id)
        self._now = now
        self._who: Who | None = None
        self._confirmed_at: float | None = None
        self._in_view: list[Any] = []  # vision.faces.Known: the grown-ups in front of the camera now
        self._names: dict[int, tuple[str, str]] = {}  # person_id -> (name, role), from matches and faces

    # ------------------------------------------------------------ evidence
    def faces(self, people: Sequence[Any]) -> None:
        """The people in front of the camera now (``vision.faces.Known``); the little one is left out."""
        grown = [p for p in people if not self._is_child(str(p.name), str(p.role))]
        self._in_view = grown
        for p in grown:
            self._names[int(p.id)] = (str(p.name), str(p.role))

    def voice(self, heard: Any) -> Who | None:
        """One turn's voice (``Heard``): who is talking now (also returned)."""
        now = self._now()
        reason = str(getattr(heard, "reason", "") or "")
        match = getattr(heard, "match", None)
        if match is not None:
            if getattr(match, "is_child", False):
                return self.current()
            self._names[int(match.person_id)] = (str(match.name), str(match.role))
            in_view = any(int(p.id) == int(match.person_id) for p in self._in_view)
            self._set(int(match.person_id), "voice and face" if in_view else "voice", sure=True, now=now)
            return self._who
        if reason == "too_short":
            return self.current()  # "yes", "okay": nothing to go on; whoever was talking still is
        if reason != "unsure":
            return self.current()  # switched off, nobody has a voiceprint ...
        best_id = getattr(heard, "best_id", None)
        best = float(getattr(heard, "best", 0.0) or 0.0)
        if best_id is None:
            return self.current()
        best_id = int(best_id)
        current = self._who
        face = self._only_grown_up()
        if face is not None and int(face.id) == best_id and best >= self.threshold - FACE_HELP_BELOW:
            self._set(best_id, "voice and face", sure=True, now=now)
        elif current is not None and current.person_id == best_id and best >= self.threshold - KEEP_BELOW:
            self._set(best_id, current.how, sure=True, now=now)  # consistent with who was talking
        elif current is not None and current.person_id != best_id and best >= self.threshold + SWITCH_DOUBT:
            self._who = dataclasses.replace(current, sure=False)  # sounds more like someone else now
        return self.current()

    def said(self, person_id: int, name: str, role: str = "") -> Who | None:
        """They said who they are ("I'm Ali") and the voice didn't say otherwise: them, sure."""
        self._names[int(person_id)] = (str(name), str(role))
        self._set(int(person_id), "what they said and their voice", sure=True, now=self._now())
        return self._who

    # ------------------------------------------------------------ the answer
    def current(self) -> Who | None:
        now = self._now()
        who = self._who
        if who is not None and self._confirmed_at is not None:
            age = now - self._confirmed_at
            if age > FORGET_S:
                self._who = who = None
            elif age > STICKY_S and who.sure:
                self._who = who = dataclasses.replace(who, sure=False)
        if who is None:
            face = self._only_grown_up()
            if face is not None:
                return Who(int(face.id), str(face.name), str(face.role), "face", sure=False)
        return who

    def _only_grown_up(self) -> Any:
        return self._in_view[0] if len(self._in_view) == 1 else None

    def _set(self, person_id: int, how: str, *, sure: bool, now: float) -> None:
        if person_id not in self._names and self._lookup is not None:
            found = self._lookup(person_id)
            if found:
                self._names[person_id] = found
        name, role = self._names.get(person_id, ("", ""))
        if name and self._is_child(name, role):
            return
        if not name:
            return
        self._who = Who(person_id, name, role, how, sure)
        self._confirmed_at = now

    def reset(self) -> None:
        self._who, self._confirmed_at, self._in_view = None, None, []


__all__ = ["FACE_HELP_BELOW", "KEEP_BELOW", "STICKY_S", "Who", "WhoTracker"]
