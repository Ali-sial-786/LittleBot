"""Activity model and the small YAML step language ("DSL") used by activity scripts.

An activity is metadata for parents (age range, category, instructions, safety
notes ...) plus a list of *steps* the engine runs in order. Steps are written
as short YAML mappings whose **first key names the step type**::

    steps:
      - say: "Hello {child_name}!"        # speak (placeholders come from the profile)
        expression: happy                 # ...optional modifiers on the same step
        pause: 2                          # any step: wait this long afterwards
      - close_eyes
      - wait: 1.5
      - prompt: "Did she clap?"            # caregiver question on the dashboard
        options: ["She clapped!", "Not this time"]
        branches:
          "She clapped!": [{sound: yay}]

Age ranges are flexible recommendations for choosing play, never a
developmental assessment. See docs/ACTIVITY_GUIDE.md for the full reference.

This module is pure (no I/O besides reading YAML files) and has no runtime
dependencies on other subsystems, so scripts can be validated anywhere.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from littlebot.core.interfaces import (
    ACTIVITY_CATEGORIES, AGE_GROUPS, EXPRESSIONS, FACE_ANIMATIONS, GESTURE_NAMES, SOUND_NAMES,
)

log = logging.getLogger("littlebot.activities.model")

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

#: Motion gestures an activity may request (optional: skipped without motion / during e-stop).
#: Every gesture the motion module knows (core.interfaces.GESTURE_NAMES: wave, nod, shrug, tilt_curious ...).
GESTURES: tuple[str, ...] = GESTURE_NAMES

#: How much an adult needs to take part.
PARTICIPATION: tuple[str, ...] = ("required", "recommended", "optional")

#: Profile lists a ``pick`` step may prefer.
PROFILE_LISTS: tuple[str, ...] = ("favourite_animals", "favourite_songs", "interests")

#: Placeholders always available in text. Values come from the parent profile with safe fallbacks.
BUILTIN_PLACEHOLDERS: dict[str, str] = {
    "child_name": "the child's nickname (fallback: 'friend')",
    "child_initial": "first letter of the nickname (fallback: 'B')",
    "favourite_animal": "first favourite animal (fallback: 'puppy')",
    "favourite_song": "first favourite song (fallback: 'Twinkle, Twinkle')",
    "robot_name": "the robot's name from the config",
    "round": "current repeat round, 1-based (1 outside a repeat)",
}

#: Caregiver replies accepted for *any* prompt (besides the option labels themselves).
CONFIRM_WORDS: tuple[str, ...] = ("done", "ok", "okay", "yes", "next", "continue")
SKIP_WORD = "skip"

LOOK_TARGETS: dict[str, tuple[float, float]] = {
    "center": (0.5, 0.5), "centre": (0.5, 0.5), "left": (0.2, 0.5), "right": (0.8, 0.5),
    "up": (0.5, 0.2), "down": (0.5, 0.8), "up_left": (0.25, 0.25), "up_right": (0.75, 0.25),
    "down_left": (0.25, 0.75), "down_right": (0.75, 0.75),
}

_NOTE_RE = re.compile(r"^(R|[A-G](?:#|b)?[0-8])$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{([^{}|]*)(?:\|([^{}]*))?\}")
_PLACEHOLDER_NAME_RE = re.compile(r"^[A-Za-z_]\w*(?:\.\w+)*$")


class ActivityError(ValueError):
    """An activity script is invalid. The message names the file, activity and step path."""


# ---------------------------------------------------------------------------
# Text templates
# ---------------------------------------------------------------------------

def template_placeholders(text: str) -> list[str]:
    """Names used in ``{placeholder}`` / ``{placeholder|fallback}`` form (``{{`` / ``}}`` are literal braces)."""
    names: list[str] = []
    for m in _PLACEHOLDER_RE.finditer(text):
        if m.group(1) is None:
            continue
        name = m.group(1).strip()
        if not _PLACEHOLDER_NAME_RE.match(name):
            raise ActivityError(f"bad placeholder '{{{m.group(1)}}}' in {text!r} (use names like {{child_name}} or {{animal.sound}})")
        names.append(name)
    return names


def to_text(value: Any) -> str:
    """Turn a variable value into speakable text (lists read naturally: 'a, b and c')."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    if isinstance(value, dict):
        for key in ("name", "word", "text", "label"):
            if key in value:
                return to_text(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        parts = [to_text(v) for v in value]
        parts = [p for p in parts if p]
        if len(parts) <= 1:
            return "".join(parts)
        return ", ".join(parts[:-1]) + " and " + parts[-1]
    return str(value)


def render_template(text: str, lookup: Callable[[str], Any], fallbacks: dict[str, str] | None = None) -> str:
    """Fill placeholders using ``lookup(name)``.

    An empty value uses the inline fallback (``{child_name|baby}``), else
    ``fallbacks[name]``, else ''. Whitespace is tidied and a value that starts
    a sentence gets a capital letter ("{sound}! {sound}!" -> "Moo! Moo!").
    """
    fallbacks = fallbacks or {}

    def repl(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok == "{{":
            return "{"
        if tok == "}}":
            return "}"
        name = m.group(1).strip()
        try:
            value = to_text(lookup(name))
        except Exception:  # noqa: BLE001 - text must never crash an activity
            log.debug("placeholder %s failed", name, exc_info=True)
            value = ""
        if not value.strip():
            value = m.group(2).strip() if m.group(2) is not None else fallbacks.get(name, "")
        before = text[: m.start()].rstrip()
        if value[:1].islower() and (not before or before[-1] in ".!?"):
            value = value[0].upper() + value[1:]  # a value that starts a sentence gets a capital
        return value

    out = _PLACEHOLDER_RE.sub(repl, text)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    out = re.sub(r"^[,.;:]\s*", "", out)
    return out


def normalise_answer(text: str) -> str:
    """Casefold, drop punctuation and squeeze spaces ("She clapped!" -> "she clapped")."""
    cleaned = re.sub(r"[^\w\s]", " ", str(text).casefold())
    return re.sub(r"\s+", " ", cleaned).strip()


# ---------------------------------------------------------------------------
# Melody notes
# ---------------------------------------------------------------------------

def parse_notes(value: Any) -> list[tuple[str, float]]:
    """Accept ``"C4 C4 G4:2 R:0.5"`` or ``[["C4", 1], "G4:2", ...]``; 'R' is a rest."""
    if isinstance(value, str):
        items: list[Any] = value.replace(",", " ").replace("|", " ").split()
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError("notes must be a string like 'C4 E4 G4:2' or a list like [[C4, 1], [G4, 2]]")
    out: list[tuple[str, float]] = []
    for item in items:
        if isinstance(item, str):
            note, _, beats_s = item.partition(":")
            try:
                beats = float(beats_s) if beats_s else 1.0
            except ValueError as e:
                raise ValueError(f"bad beats in note {item!r}") from e
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            note, beats = str(item[0]), item[1]
            try:
                beats = float(beats)
            except (TypeError, ValueError) as e:
                raise ValueError(f"bad beats in note {item!r}") from e
        else:
            raise ValueError(f"bad note {item!r}: use 'C4', 'C4:2' or [C4, 2]")
        note = note.strip()
        if not _NOTE_RE.match(note):
            raise ValueError(f"bad note name {note!r}: use a letter A-G, optional # or b, and an octave 0-8 (e.g. C4, F#4) or R for a rest")
        if not 0 < beats <= 8:
            raise ValueError(f"note {note!r}: beats must be between 0 and 8")
        out.append((note, beats))
    if not out:
        raise ValueError("a melody needs at least one note")
    if len(out) > 400:
        raise ValueError("a melody may have at most 400 notes")
    return out


def melody_seconds(notes: Iterable[tuple[str, float]], tempo_bpm: float) -> float:
    return sum(b for _n, b in notes) * 60.0 / max(1.0, tempo_bpm)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class Step(BaseModel):
    """Base class for all steps. ``pause`` (seconds) waits after the step finishes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    pause: float | None = Field(default=None, ge=0, le=60)

    #: field that receives the value in the short form ``- say: "Hi"``
    PRIMARY: ClassVar[str | None] = None
    #: fields holding a list of steps / a {key: steps} map / a list of step lists
    NESTED: ClassVar[tuple[str, ...]] = ()
    NESTED_MAPS: ClassVar[tuple[str, ...]] = ()
    NESTED_LISTS: ClassVar[tuple[str, ...]] = ()

    def templates(self) -> list[str]:
        """Text that may contain placeholders (validated at load time)."""
        return []

    def children(self) -> list[tuple[str, list["Step"]]]:
        out: list[tuple[str, list[Step]]] = []
        for name in self.NESTED:
            out.append((name, getattr(self, name)))
        for name in self.NESTED_MAPS:
            for key, steps in getattr(self, name).items():
                out.append((f"{name}[{key!r}]", steps))
        for name in self.NESTED_LISTS:
            for i, steps in enumerate(getattr(self, name)):
                out.append((f"{name}[{i}]", steps))
        return out

    def summary(self) -> str:
        """Short human description (dashboard / logs)."""
        return ""


def _check_expression(v: str | None) -> str | None:
    if v is not None and v not in EXPRESSIONS:
        raise ValueError(f"unknown expression {v!r}; choose one of: {', '.join(EXPRESSIONS)}")
    return v


def _check_animation(v: str | None) -> str | None:
    if v is not None and v not in FACE_ANIMATIONS:
        raise ValueError(f"unknown animation {v!r}; choose one of: {', '.join(FACE_ANIMATIONS)}")
    return v


def _check_ident(v: str, what: str = "variable name") -> str:
    if not _IDENT_RE.match(v):
        raise ValueError(f"bad {what} {v!r}: use letters, digits and _ (not starting with a digit)")
    if v in BUILTIN_PLACEHOLDERS:
        raise ValueError(f"{v!r} is a built-in placeholder and cannot be overwritten")
    return v


class SayStep(Step):
    """Speak a sentence (a list = pick one at random each time)."""

    kind: Literal["say"] = "say"
    text: str | list[str]
    expression: str | None = None  # set before speaking
    animation: str | None = None  # played before speaking
    rate: float | None = Field(default=None, ge=0.5, le=1.5)
    PRIMARY: ClassVar[str | None] = "text"

    @field_validator("text")
    @classmethod
    def _text(cls, v: str | list[str]) -> str | list[str]:
        items = [v] if isinstance(v, str) else v
        if not items or any(not str(t).strip() for t in items):
            raise ValueError("say needs non-empty text")
        if any(len(t) > 300 for t in items):
            raise ValueError("keep each sentence under 300 characters (short phrases work best)")
        return v

    _expr = field_validator("expression")(classmethod(lambda cls, v: _check_expression(v)))
    _anim = field_validator("animation")(classmethod(lambda cls, v: _check_animation(v)))

    def templates(self) -> list[str]:
        return [self.text] if isinstance(self.text, str) else list(self.text)

    def summary(self) -> str:
        return self.text if isinstance(self.text, str) else self.text[0]


class ExpressionStep(Step):
    kind: Literal["expression"] = "expression"
    name: str
    hold: float | None = Field(default=None, ge=0, le=60)
    PRIMARY: ClassVar[str | None] = "name"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_expression(v)  # type: ignore[return-value]


class AnimationStep(Step):
    kind: Literal["animation"] = "animation"
    name: str
    PRIMARY: ClassVar[str | None] = "name"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _check_animation(v)  # type: ignore[return-value]


class SoundStep(Step):
    kind: Literal["sound"] = "sound"
    name: str
    wait: bool = True
    volume: float | None = Field(default=None, ge=0, le=1)
    PRIMARY: ClassVar[str | None] = "name"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if v in SOUND_NAMES:
            return v
        if re.match(r"^[\w\-/]+\.(wav|ogg)$", v) and ".." not in v and not v.startswith("/"):
            return v  # a file under assets/sounds
        raise ValueError(f"unknown sound {v!r}; choose one of: {', '.join(SOUND_NAMES)} (or a .wav/.ogg file under assets/sounds)")


class MelodyStep(Step):
    kind: Literal["melody"] = "melody"
    notes: list[tuple[str, float]]
    tempo: float = Field(default=100, ge=40, le=240)
    wait: bool = True
    title: str | None = None
    PRIMARY: ClassVar[str | None] = "notes"

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v: Any) -> list[tuple[str, float]]:
        return parse_notes(v)

    def summary(self) -> str:
        return self.title or ""


class GestureStep(Step):
    """Optional body movement. Skipped silently without motion hardware or during an e-stop."""

    kind: Literal["gesture"] = "gesture"
    name: str
    wait: bool = False  # default: move while the next steps continue
    PRIMARY: ClassVar[str | None] = "name"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if v not in GESTURES:
            raise ValueError(f"unknown gesture {v!r}; choose one of: {', '.join(GESTURES)}")
        return v


class LookStep(Step):
    """Look somewhere: ``look: left`` / ``look: [0.3, 0.4]`` / ``look: {x: 0.3, y: 0.4}``."""

    kind: Literal["look"] = "look"
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    hold: float | None = Field(default=None, ge=0, le=60)
    PRIMARY: ClassVar[str | None] = "target"

    @model_validator(mode="before")
    @classmethod
    def _target(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "target" not in data:
            return data
        data = dict(data)
        target = data.pop("target")
        if isinstance(target, str):
            if target not in LOOK_TARGETS:
                raise ValueError(f"unknown look target {target!r}; use one of {', '.join(LOOK_TARGETS)} or [x, y]")
            data["x"], data["y"] = LOOK_TARGETS[target]
        elif isinstance(target, (list, tuple)) and len(target) == 2:
            data["x"], data["y"] = target
        elif isinstance(target, dict):
            data.update(target)
        else:
            raise ValueError("look needs a direction (left/right/up/down/center) or [x, y] between 0 and 1")
        return data


class ClearGazeStep(Step):
    kind: Literal["clear_gaze"] = "clear_gaze"


class CloseEyesStep(Step):
    kind: Literal["close_eyes"] = "close_eyes"


class OpenEyesStep(Step):
    kind: Literal["open_eyes"] = "open_eyes"


class BlinkStep(Step):
    kind: Literal["blink"] = "blink"
    double: bool = False
    PRIMARY: ClassVar[str | None] = "double"


class WaitStep(Step):
    """Wait (scaled by the clock; frozen while paused). Gives the child time to respond."""

    kind: Literal["wait"] = "wait"
    seconds: float = Field(ge=0, le=120)
    PRIMARY: ClassVar[str | None] = "seconds"


class NoteStep(Step):
    """A tip shown to the caregiver on the dashboard (not spoken)."""

    kind: Literal["note"] = "note"
    text: str = Field(min_length=1, max_length=300)
    PRIMARY: ClassVar[str | None] = "text"

    def templates(self) -> list[str]:
        return [self.text]

    def summary(self) -> str:
        return self.text


def _check_options(options: list[str]) -> list[str]:
    if not 1 <= len(options) <= 6:
        raise ValueError("a prompt needs 1 to 6 options")
    seen: set[str] = set()
    for o in options:
        if not str(o).strip():
            raise ValueError("prompt options must not be empty")
        if len(o) > 40:
            raise ValueError(f"option {o!r} is too long (max 40 characters)")
        n = normalise_answer(o)
        if n in seen:
            raise ValueError(f"duplicate option {o!r}")
        if n == SKIP_WORD:
            raise ValueError("'skip' is always available; do not list it as an option")
        seen.add(n)
    return options


class PromptStep(Step):
    """Ask the caregiver (dashboard buttons). Branch on the answer; ``on_timeout`` if nobody answers."""

    kind: Literal["prompt"] = "prompt"
    text: str = Field(min_length=1, max_length=200)
    options: list[str] = Field(default_factory=lambda: ["Done"])
    timeout: float | None = Field(default=None, ge=1, le=600)  # None: config activities.caregiver_timeout_seconds
    branches: dict[str, list[Step]] = Field(default_factory=dict)
    on_timeout: list[Step] = Field(default_factory=list)
    PRIMARY: ClassVar[str | None] = "text"
    NESTED: ClassVar[tuple[str, ...]] = ("on_timeout",)
    NESTED_MAPS: ClassVar[tuple[str, ...]] = ("branches",)

    _opts = field_validator("options")(classmethod(lambda cls, v: _check_options(v)))

    @model_validator(mode="after")
    def _branch_keys(self) -> "PromptStep":
        by_norm = {normalise_answer(o): o for o in self.options}
        for key in self.branches:
            if normalise_answer(key) not in by_norm:
                raise ValueError(f"branch {key!r} does not match any option ({', '.join(self.options)})")
        return self

    def branch_for(self, option: str) -> list[Step]:
        n = normalise_answer(option)
        for key, steps in self.branches.items():
            if normalise_answer(key) == n:
                return steps
        return []

    def templates(self) -> list[str]:
        return [self.text, *self.options]

    def summary(self) -> str:
        return self.text


class WaitForStep(Step):
    """Wait for a bus event (e.g. ``vision.gesture`` with ``match: {gesture: wave}``).

    With ``ask_caregiver`` a caregiver prompt is shown at the same time, so the
    step also works without camera / microphone: whichever comes first wins.
    """

    kind: Literal["wait_for"] = "wait_for"
    event: list[str]
    match: dict[str, Any] = Field(default_factory=dict)  # payload key == value (case-insensitive for text)
    contains: dict[str, list[str]] = Field(default_factory=dict)  # payload text contains any word
    timeout: float = Field(default=10, ge=0.5, le=600)
    ask_caregiver: str | None = Field(default=None, max_length=200)
    caregiver_option: str = Field(default="Done", min_length=1, max_length=40)
    on_event: list[Step] = Field(default_factory=list)
    on_timeout: list[Step] = Field(default_factory=list)
    PRIMARY: ClassVar[str | None] = "event"
    NESTED: ClassVar[tuple[str, ...]] = ("on_event", "on_timeout")

    @field_validator("event", mode="before")
    @classmethod
    def _events(cls, v: Any) -> list[str]:
        items = [v] if isinstance(v, str) else list(v or [])
        if not items:
            raise ValueError("wait_for needs an event topic such as vision.gesture")
        for t in items:
            if not isinstance(t, str) or not re.match(r"^[a-z_*][a-z0-9_.*]*$", t):
                raise ValueError(f"bad event pattern {t!r} (e.g. vision.gesture, audio.utterance, vision.*)")
            if t.startswith(("activity.", "system.", "caregiver.")) or t == "*":
                raise ValueError(f"wait_for cannot wait on {t!r}; use a prompt for caregiver answers")
        return items

    @field_validator("contains", mode="before")
    @classmethod
    def _contains(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {k: ([w] if isinstance(w, str) else w) for k, w in v.items()}
        return v

    def templates(self) -> list[str]:
        out = [self.caregiver_option]
        if self.ask_caregiver:
            out.append(self.ask_caregiver)
        for words in self.contains.values():
            out.extend(words)
        return out

    def summary(self) -> str:
        return self.ask_caregiver or ""


class ListenStep(Step):
    """Listen once (microphone). Works when the mic is off: that simply counts as a timeout.

    Heard text is never logged or stored; ``store_as`` keeps it in memory for this run only.
    """

    kind: Literal["listen"] = "listen"
    timeout: float = Field(default=6, ge=1, le=30)
    expect: list[str] = Field(default_factory=list)  # words (placeholders allowed)
    store_as: str | None = None
    on_match: list[Step] = Field(default_factory=list)  # heard an expected word
    on_heard: list[Step] = Field(default_factory=list)  # heard anything (or no on_match)
    on_timeout: list[Step] = Field(default_factory=list)  # nothing heard / mic off
    PRIMARY: ClassVar[str | None] = "timeout"
    NESTED: ClassVar[tuple[str, ...]] = ("on_match", "on_heard", "on_timeout")

    @field_validator("store_as")
    @classmethod
    def _store(cls, v: str | None) -> str | None:
        return None if v is None else _check_ident(v)

    def templates(self) -> list[str]:
        return list(self.expect)


class RepeatStep(Step):
    kind: Literal["repeat"] = "repeat"
    times: int = Field(ge=1, le=20)
    steps: list[Step] = Field(min_length=1)
    PRIMARY: ClassVar[str | None] = "times"
    NESTED: ClassVar[tuple[str, ...]] = ("steps",)


class ChoiceStep(Step):
    """Run ONE of the options, picked at random (seeded RNG -> reproducible in tests)."""

    kind: Literal["choice"] = "choice"
    options: list[list[Step]] = Field(min_length=1)
    weights: list[float] | None = None
    avoid_repeats: bool = True  # use every option once before repeating one (within a run)
    PRIMARY: ClassVar[str | None] = "options"
    NESTED_LISTS: ClassVar[tuple[str, ...]] = ("options",)

    @model_validator(mode="after")
    def _weights(self) -> "ChoiceStep":
        if self.weights is not None:
            if len(self.weights) != len(self.options):
                raise ValueError("weights must have one number per option")
            if any(w < 0 for w in self.weights) or sum(self.weights) <= 0:
                raise ValueError("weights must be >= 0 and not all zero")
        return self


class SetStep(Step):
    """Set variables: ``set: {animal: cow, count: 3}`` (text values may use placeholders)."""

    kind: Literal["set"] = "set"
    values: dict[str, Any] = Field(min_length=1)
    PRIMARY: ClassVar[str | None] = "values"

    @field_validator("values")
    @classmethod
    def _keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k in v:
            _check_ident(k)
        return v

    def templates(self) -> list[str]:
        return [v for v in self.values.values() if isinstance(v, str)]


class PickStep(Step):
    """Pick random item(s) from a list into a variable.

    ``from`` is an inline list or the name of a data list (activity ``data:`` or
    assets/activities/_data.yaml). ``prefer_profile`` puts the child's favourites
    first (e.g. favourite animals). Repeats are avoided until the list runs out.
    """

    kind: Literal["pick"] = "pick"
    var: str
    from_: str | list[Any] = Field(alias="from")
    prefer_profile: Literal["favourite_animals", "favourite_songs", "interests"] | None = None
    count: int = Field(default=1, ge=1, le=10)
    avoid_repeats: bool = True
    PRIMARY: ClassVar[str | None] = "var"

    _var = field_validator("var")(classmethod(lambda cls, v: _check_ident(v)))

    @field_validator("from_")
    @classmethod
    def _from(cls, v: str | list[Any]) -> str | list[Any]:
        if isinstance(v, list) and not v:
            raise ValueError("pick needs a non-empty list")
        if isinstance(v, str) and not _IDENT_RE.match(v):
            raise ValueError(f"bad data list name {v!r}")
        return v


class ForEachStep(Step):
    """Run ``steps`` once per item of a list, in order (``{round}`` = 1-based position).

    ``in`` is an inline list or a data list name; ``shuffle`` randomises the order,
    ``limit`` caps how many items are used.
    """

    kind: Literal["for_each"] = "for_each"
    var: str
    in_: str | list[Any] = Field(alias="in")
    steps: list[Step] = Field(min_length=1)
    shuffle: bool = False
    limit: int | None = Field(default=None, ge=1, le=20)
    PRIMARY: ClassVar[str | None] = "var"
    NESTED: ClassVar[tuple[str, ...]] = ("steps",)

    _var = field_validator("var")(classmethod(lambda cls, v: _check_ident(v)))

    @field_validator("in_")
    @classmethod
    def _in(cls, v: str | list[Any]) -> str | list[Any]:
        if isinstance(v, list) and not v:
            raise ValueError("for_each needs a non-empty list")
        if isinstance(v, list) and len(v) > 40:
            raise ValueError("for_each lists are limited to 40 items")
        if isinstance(v, str) and not _IDENT_RE.match(v):
            raise ValueError(f"bad data list name {v!r}")
        return v


class SongLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    words: str | None = Field(default=None, max_length=200)
    notes: list[tuple[str, float]] = Field(default_factory=list)

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v: Any) -> Any:
        return parse_notes(v) if v not in (None, [], "") else []

    @model_validator(mode="after")
    def _something(self) -> "SongLine":
        if not self.words and not self.notes:
            raise ValueError("a song line needs words, notes or both")
        return self


class SongStep(Step):
    """A song, line by line. Each line's words are shown on the dashboard so the caregiver can sing along.

    style: speak_then_play (default) | play_then_speak | play_only | speak_only
    """

    kind: Literal["song"] = "song"
    title: str = Field(min_length=1, max_length=80)
    tempo: float = Field(default=100, ge=40, le=240)
    style: Literal["speak_then_play", "play_then_speak", "play_only", "speak_only"] = "speak_then_play"
    lines: list[SongLine] = Field(min_length=1)
    PRIMARY: ClassVar[str | None] = "title"

    def templates(self) -> list[str]:
        return [self.title] + [ln.words for ln in self.lines if ln.words]

    def summary(self) -> str:
        return self.title


class EndStep(Step):
    """Finish the activity now (counts as completed)."""

    kind: Literal["end"] = "end"
    reason: str = Field(default="completed", max_length=80)
    PRIMARY: ClassVar[str | None] = "reason"


STEP_TYPES: dict[str, type[Step]] = {
    cls.model_fields["kind"].default: cls
    for cls in (
        SayStep, ExpressionStep, AnimationStep, SoundStep, MelodyStep, GestureStep, LookStep, ClearGazeStep,
        CloseEyesStep, OpenEyesStep, BlinkStep, WaitStep, NoteStep, PromptStep, WaitForStep, ListenStep,
        RepeatStep, ChoiceStep, SetStep, PickStep, ForEachStep, SongStep, EndStep,
    )
}

#: kinds whose short-form value is itself a mapping (not merged as modifiers)
_MAPPING_PRIMARY = {"set", "look"}


# ---------------------------------------------------------------------------
# Step parsing (YAML -> models) with readable error paths
# ---------------------------------------------------------------------------

def _format_validation(err: ValidationError, path: str) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "__root__")
        msg = str(e.get("msg", "invalid")).removeprefix("Value error, ")
        if loc and msg.startswith(loc + "["):  # nested step error already carries its path
            parts.append(f"{path}{'.' if path else ''}{msg}")
            continue
        parts.append(f"{path}{'.' + loc if loc else ''}: {msg}")
    return "; ".join(parts)


def normalise_step(raw: Any, path: str) -> dict[str, Any]:
    """YAML short forms -> ``{"kind": ..., fields...}`` (first key = step type)."""
    known = ", ".join(STEP_TYPES)
    if isinstance(raw, str):
        kind = raw.strip()
        if kind not in STEP_TYPES:
            raise ActivityError(f"{path}: unknown step {raw!r}; step types are: {known}")
        return {"kind": kind}
    if not isinstance(raw, dict) or not raw:
        raise ActivityError(f"{path}: a step must be a mapping such as '- say: Hello' (got {type(raw).__name__})")
    if "kind" in raw:
        kind = raw["kind"]
        if kind not in STEP_TYPES:
            raise ActivityError(f"{path}: unknown step kind {kind!r}; step types are: {known}")
        return dict(raw)
    kind = next(iter(raw))
    if kind not in STEP_TYPES:
        raise ActivityError(f"{path}: unknown step type {kind!r} (the first key names the step); step types are: {known}")
    cls = STEP_TYPES[kind]
    value = raw[kind]
    rest = {k: v for k, v in raw.items() if k != kind}
    out: dict[str, Any] = {"kind": kind}
    if isinstance(value, dict) and kind not in _MAPPING_PRIMARY:
        clash = set(value) & set(rest)
        if clash:
            raise ActivityError(f"{path}: {sorted(clash)} given twice")
        out.update(value)
    elif value is not None:
        if cls.PRIMARY is None:
            raise ActivityError(f"{path}: '{kind}' takes no value (write just '- {kind}')")
        out[cls.PRIMARY] = value
    out.update(rest)
    return out


def _as_step_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, dict) and set(raw) == {"steps"}:
        raw = raw["steps"]
    if isinstance(raw, (str, dict)):
        return [raw]
    if isinstance(raw, list):
        return raw
    raise ActivityError(f"expected a list of steps, got {type(raw).__name__}")


def parse_steps(raw: Any, path: str = "steps") -> list[Step]:
    """Parse a list (or a single step) into Step models. Raises ActivityError with the step path."""
    if raw and all(isinstance(s, Step) for s in (raw if isinstance(raw, list) else [raw])):
        return list(raw) if isinstance(raw, list) else [raw]
    try:
        items = _as_step_list(raw)
    except ActivityError as e:
        raise ActivityError(f"{path}: {e}") from None
    return [parse_step(item, f"{path}[{i}]") for i, item in enumerate(items)]


def parse_step(raw: Any, path: str) -> Step:
    if isinstance(raw, Step):
        return raw
    data = normalise_step(raw, path)
    kind = data["kind"]
    cls = STEP_TYPES[kind]
    where = f"{path} ({kind})"
    for name in cls.NESTED:
        if name in data:
            data[name] = parse_steps(data[name], f"{where}.{name}")
    for name in cls.NESTED_MAPS:
        if name in data:
            value = data[name]
            if not isinstance(value, dict):
                raise ActivityError(f"{where}.{name}: expected a mapping of option -> steps")
            data[name] = {str(k): parse_steps(v, f"{where}.{name}[{k!r}]") for k, v in value.items()}
    for name in cls.NESTED_LISTS:
        if name in data:
            value = data[name]
            if not isinstance(value, list):
                raise ActivityError(f"{where}.{name}: expected a list of options")
            data[name] = [parse_steps(v, f"{where}.{name}[{i}]") for i, v in enumerate(value)]
    try:
        return cls.model_validate(data)
    except ValidationError as e:
        raise ActivityError(_format_validation(e, where)) from None


def iter_steps(steps: list[Step], path: str = "steps") -> Iterator[tuple[str, Step]]:
    """Depth-first walk over every step (including nested branches)."""
    for i, step in enumerate(steps):
        here = f"{path}[{i}]"
        yield here, step
        for name, child in step.children():
            yield from iter_steps(child, f"{here}.{name}")


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------

class Activity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=300)
    min_age_months: int = Field(ge=0, le=120)
    max_age_months: int = Field(ge=1, le=144)
    category: str
    participation: Literal["required", "recommended", "optional"] = "recommended"
    parent_instructions: str = Field(min_length=1, max_length=1500)
    safety_notes: list[str] = Field(min_length=1)
    pausable: bool = True
    duration_seconds: int = Field(ge=10, le=3600)
    finish_expression: str = "happy"  # shown briefly when the activity completes (e.g. sleepy for a bedtime story)
    tags: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, list[Any]] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    source: str = ""  # file the activity was loaded from (set by the loader)

    @field_validator("steps", mode="before")
    @classmethod
    def _steps(cls, v: Any) -> list[Step]:
        return parse_steps(v, "steps")

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        if v not in ACTIVITY_CATEGORIES:
            raise ValueError(f"unknown category {v!r}; choose one of: {', '.join(ACTIVITY_CATEGORIES)}")
        return v

    @field_validator("finish_expression")
    @classmethod
    def _finish_expr(cls, v: str) -> str:
        return _check_expression(v)  # type: ignore[return-value]

    @field_validator("variables")
    @classmethod
    def _vars(cls, v: dict[str, Any]) -> dict[str, Any]:
        for k in v:
            _check_ident(k)
        return v

    @field_validator("data")
    @classmethod
    def _data(cls, v: dict[str, list[Any]]) -> dict[str, list[Any]]:
        for k, items in v.items():
            if not _IDENT_RE.match(k):
                raise ValueError(f"bad data list name {k!r}")
            if not items:
                raise ValueError(f"data list {k!r} is empty")
        return v

    @field_validator("safety_notes")
    @classmethod
    def _notes(cls, v: list[str]) -> list[str]:
        if any(not str(n).strip() for n in v):
            raise ValueError("safety notes must not be empty")
        return v

    @model_validator(mode="after")
    def _ages(self) -> "Activity":
        if self.max_age_months <= self.min_age_months:
            raise ValueError("max_age_months must be greater than min_age_months")
        return self

    # -- helpers ---------------------------------------------------------
    def age_groups(self) -> list[str]:
        """Labels of the (flexible) age groups this activity's range overlaps."""
        labels = [label for _k, lo, hi, label in AGE_GROUPS if self.min_age_months < hi and lo < self.max_age_months]
        if self.max_age_months > AGE_GROUPS[-1][2]:
            labels.append("5 years +")
        return labels

    def age_range_label(self) -> str:
        return age_range_label(self.min_age_months, self.max_age_months)

    def suitable_for(self, age_months: int) -> bool:
        return self.min_age_months <= age_months < self.max_age_months

    def all_steps(self) -> Iterator[tuple[str, Step]]:
        return iter_steps(self.steps)


def age_range_label(lo: int, hi: int) -> str:
    """Human label; like AGE_GROUPS labels the upper bound is exclusive ("3–5 years" = 36 to <60 months)."""

    def years(m: int) -> str:
        whole, rest = divmod(m, 12)
        return f"{whole}½" if rest == 6 else f"{m / 12:g}"

    if hi <= 24:
        return f"{lo}–{hi} months"
    if lo >= 24:
        return f"{years(lo)}–{years(hi)} years"
    return f"{lo} months – {years(hi)} years"


# ---------------------------------------------------------------------------
# Cross-reference checks (placeholders, data lists)
# ---------------------------------------------------------------------------

def _item_keys(items: list[Any]) -> set[str] | None:
    """Keys common to every dict item (None if the list holds plain values)."""
    if not items or not all(isinstance(i, dict) for i in items):
        return None
    keys = set(items[0])
    for i in items[1:]:
        keys &= set(i)
    return keys


def check_activity(activity: Activity, shared_data: dict[str, list[Any]] | None = None) -> list[str]:
    """Validate placeholders and data references. Returns error messages (empty = OK)."""
    shared_data = shared_data or {}
    data = {**shared_data, **activity.data}
    errors: list[str] = []
    declared: set[str] = set(activity.variables)
    fields: dict[str, set[str] | None] = {k: (set(v) if isinstance(v, dict) else None) for k, v in activity.variables.items()}
    steps = list(activity.all_steps())
    for path, step in steps:
        if isinstance(step, SetStep):
            declared.update(step.values)
            for k, v in step.values.items():
                fields.setdefault(k, set(v) if isinstance(v, dict) else None)
        elif isinstance(step, (PickStep, ForEachStep)):
            declared.add(step.var)
            source = step.from_ if isinstance(step, PickStep) else step.in_
            if isinstance(source, str):
                if source not in data:
                    errors.append(f"{path} ({step.kind}): unknown data list {source!r} (known: {', '.join(sorted(data)) or 'none'})")
                    continue
                items = data[source]
            else:
                items = source
            single = not isinstance(step, PickStep) or step.count == 1
            keys = _item_keys(items) if single else None
            if step.var in fields and fields[step.var] is not None and keys is not None:
                fields[step.var] = fields[step.var] & keys  # type: ignore[operator]
            else:
                fields[step.var] = keys if step.var not in fields else None
        elif isinstance(step, ListenStep) and step.store_as:
            declared.add(step.store_as)
            fields[step.store_as] = None
    known = set(BUILTIN_PLACEHOLDERS) | declared
    for path, step in steps:
        for text in step.templates():
            try:
                names = template_placeholders(text)
            except ActivityError as e:
                errors.append(f"{path} ({step.kind}): {e}")
                continue
            for name in names:
                root, _, sub = name.partition(".")
                if root not in known:
                    errors.append(f"{path} ({step.kind}): unknown placeholder {{{name}}}; "
                                  f"available: {', '.join(sorted(known))}")
                elif sub and fields.get(root) is not None and sub.split(".")[0] not in fields[root]:  # type: ignore[operator]
                    errors.append(f"{path} ({step.kind}): {{{name}}}: {root!r} items have no field {sub!r} "
                                  f"(fields: {', '.join(sorted(fields[root] or ()))})")
    return errors


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

try:  # the C loader is much faster on the Pi when available
    _Loader: Any = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader


@dataclass
class LoadReport:
    activities: dict[str, Activity] = field(default_factory=dict)
    shared_data: dict[str, list[Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


def _activity_dicts(doc: Any, source: str) -> list[dict[str, Any]]:
    if doc is None:
        return []
    if isinstance(doc, dict) and "activities" in doc:
        items = doc["activities"]
        if not isinstance(items, list):
            raise ActivityError(f"{source}: 'activities' must be a list")
    elif isinstance(doc, dict):
        items = [doc]
    elif isinstance(doc, list):
        items = doc
    else:
        raise ActivityError(f"{source}: expected an activity mapping or an 'activities:' list")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ActivityError(f"{source}: activity #{i + 1} must be a mapping")
    return items


def parse_activity(raw: dict[str, Any], source: str = "", shared_data: dict[str, list[Any]] | None = None) -> Activity:
    """Validate one activity mapping. Raises ActivityError with a readable message."""
    label = f"{source}: " if source else ""
    act_id = raw.get("id", "?") if isinstance(raw, dict) else "?"
    try:
        act = Activity.model_validate({**raw, "source": source})
    except ValidationError as e:
        raise ActivityError(f"{label}activity {act_id!r}: {_format_validation(e, '').lstrip('.')}") from None
    except ActivityError as e:
        raise ActivityError(f"{label}activity {act_id!r}: {e}") from None
    problems = check_activity(act, shared_data)
    if problems:
        raise ActivityError(f"{label}activity {act_id!r}: " + "; ".join(problems))
    return act


def load_yaml_text(text: str, source: str = "<text>") -> Any:
    try:
        return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader / CSafeLoader only
    except yaml.YAMLError as e:
        raise ActivityError(f"{source}: YAML syntax error: {e}") from None


def parse_activities_text(text: str, source: str = "<text>", shared_data: dict[str, list[Any]] | None = None) -> list[Activity]:
    """Parse YAML text holding one activity or an ``activities:`` list (strict)."""
    return [parse_activity(d, source, shared_data) for d in _activity_dicts(load_yaml_text(text, source), source)]


def _script_files(directory: Path) -> list[Path]:
    return sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])


def load_directories(dirs: Iterable[Path | str]) -> LoadReport:
    """Load every script in ``dirs`` (files starting with '_' hold shared ``data:`` lists).

    Never raises for bad content: problems are collected in ``report.errors`` and
    the broken activity is skipped, so one typo cannot take the robot down.
    """
    report = LoadReport()
    paths = [Path(d) for d in dirs]
    files: list[Path] = []
    for d in paths:
        if d.is_dir():
            files.extend(_script_files(d))
        elif d.is_file():
            files.append(d)
    shared = [f for f in files if f.name.startswith("_")]
    scripts = [f for f in files if not f.name.startswith("_")]
    for f in shared:
        report.files.append(str(f))
        try:
            doc = load_yaml_text(f.read_text(encoding="utf-8"), f.name) or {}
            data = doc.get("data") if isinstance(doc, dict) else None
            if not isinstance(data, dict):
                raise ActivityError(f"{f.name}: a shared data file needs a top-level 'data:' mapping of lists")
            for key, items in data.items():
                if not _IDENT_RE.match(str(key)) or not isinstance(items, list) or not items:
                    raise ActivityError(f"{f.name}: data.{key} must be a non-empty list")
                report.shared_data[str(key)] = items
        except (OSError, ActivityError) as e:
            report.errors.append(str(e))
    for f in scripts:
        report.files.append(str(f))
        try:
            dicts = _activity_dicts(load_yaml_text(f.read_text(encoding="utf-8"), f.name), f.name)
        except (OSError, ActivityError) as e:
            report.errors.append(str(e))
            continue
        for raw in dicts:
            try:
                act = parse_activity(raw, f.name, report.shared_data)
            except ActivityError as e:
                report.errors.append(str(e))
                continue
            if act.id in report.activities:
                report.errors.append(f"{f.name}: duplicate activity id {act.id!r} "
                                     f"(already defined in {Path(report.activities[act.id].source).name}); skipped")
                continue
            report.activities[act.id] = act
    return report


class ActivityLibrary:
    """The set of known activities (loaded from YAML folders, or given directly)."""

    def __init__(self, dirs: Iterable[Path | str] = (), *, activities: Iterable[Activity] = (),
                 shared_data: dict[str, list[Any]] | None = None) -> None:
        self.dirs = [Path(d) for d in dirs]
        self._activities: dict[str, Activity] = {a.id: a for a in activities}
        self.shared_data: dict[str, list[Any]] = dict(shared_data or {})
        self.errors: list[str] = []
        self.files: list[str] = []

    @classmethod
    def from_yaml(cls, text: str, *, shared_data: dict[str, list[Any]] | None = None) -> "ActivityLibrary":
        acts = parse_activities_text(text, "<text>", shared_data)
        return cls(activities=acts, shared_data=shared_data)

    def load(self, *, strict: bool = False) -> list[str]:
        """(Re)load from ``dirs``. Returns errors; with ``strict`` raises ActivityError instead."""
        report = load_directories(self.dirs)
        if strict and report.errors:
            raise ActivityError("\n".join(report.errors))
        self._activities = report.activities
        self.shared_data = report.shared_data
        self.errors = report.errors
        self.files = report.files
        for err in report.errors:
            log.error("activity script problem: %s", err)
        return list(report.errors)

    def get(self, activity_id: str) -> Activity | None:
        return self._activities.get(activity_id)

    def all(self) -> list[Activity]:
        return list(self._activities.values())

    def ids(self) -> list[str]:
        return list(self._activities)

    def __len__(self) -> int:
        return len(self._activities)

    def __contains__(self, activity_id: object) -> bool:
        return activity_id in self._activities

    def data_list(self, name: str, activity: Activity | None = None) -> list[Any]:
        if activity is not None and name in activity.data:
            return list(activity.data[name])
        return list(self.shared_data.get(name, []))
