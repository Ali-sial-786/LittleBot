"""Mouth poses: what the mouth looks like at rest and for each spoken mouth shape (viseme).

Pure data and arithmetic (no pygame). The animator blends the expression's resting mouth
(:class:`~littlebot.face.expressions.MouthParams`) with a *spoken* pose while LittleBot talks;
the spoken pose follows the viseme of the sound being played right now (``audio.level``
events carry ``{"level", "shape"}``, see ``littlebot.audio.dsp.viseme_track``).
"""

from __future__ import annotations

from dataclasses import dataclass

# Same names as littlebot.audio.dsp.VISEMES (kept separate so the face does not import audio).
VISEMES: tuple[str, ...] = ("closed", "small", "ah", "ee", "oo")


@dataclass(frozen=True)
class MouthPose:
    """A mouth shape. ``width`` multiplies ``frame.MOUTH_W``; the others are 0..1 (smile -1..1)."""

    open: float = 0.0
    width: float = 1.0
    smile: float = 0.45
    round: float = 0.0
    teeth: float = 0.0
    tongue: float = 0.0


# Spoken shapes: how open / wide / round, and whether the teeth show. The smile comes from the
# expression (a sad LittleBot still talks with a slightly sad mouth).
VISEME_POSES: dict[str, MouthPose] = {
    "closed": MouthPose(open=0.0, width=0.92),  # silence, m / b / p: lips together
    "small": MouthPose(open=0.3, width=0.9, round=0.15),  # most consonants, "uh"
    "ah": MouthPose(open=0.95, width=1.06),  # "a" in father: wide open
    "ee": MouthPose(open=0.36, width=1.3, teeth=1.0),  # "ee", "eh", s / z / sh / f: wide, teeth
    "oo": MouthPose(open=0.56, width=0.6, round=1.0),  # "oo", "oh", w: round
}
assert set(VISEME_POSES) == set(VISEMES)


def level_viseme(level: float) -> str:
    """Shape for a bare loudness (old ``{"level"}`` events, or the synthesized talking pulse)."""
    if level < 0.12:
        return "closed"
    return "small" if level < 0.45 else "ah"


def spoken_pose(shape: str, level: float, rest: MouthPose) -> MouthPose:
    """The talking mouth for ``shape`` at loudness ``level`` (0..1), in the mood of ``rest``."""
    pose = VISEME_POSES.get(shape) or VISEME_POSES[level_viseme(level)]
    level = 0.0 if level < 0.0 else 1.0 if level > 1.0 else level
    open_ = pose.open * (0.45 + 0.55 * level) if pose.open > 0.0 else 0.0
    return MouthPose(
        open=open_,
        width=pose.width * (0.8 + 0.2 * rest.width),
        smile=0.2 + 0.5 * rest.smile,
        round=pose.round,
        teeth=pose.teeth,
        tongue=0.0,
    )


def lerp_pose(a: MouthPose, b: MouthPose, t: float) -> MouthPose:
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return MouthPose(
        open=a.open + (b.open - a.open) * t,
        width=a.width + (b.width - a.width) * t,
        smile=a.smile + (b.smile - a.smile) * t,
        round=a.round + (b.round - a.round) * t,
        teeth=a.teeth + (b.teeth - a.teeth) * t,
        tongue=a.tongue + (b.tongue - a.tongue) * t,
    )


# Asleep: a tiny round "o" that breathes.
SLEEP_POSE = MouthPose(open=0.14, width=0.4, smile=0.0, round=1.0)
