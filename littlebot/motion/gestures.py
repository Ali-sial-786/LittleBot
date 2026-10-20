"""Gesture library: short keyframe sequences in normalised joint positions.

Positions are normalised per joint (see ``JointSpec.resolve``): 0 = neutral, +1 = the
limit named by ``positive_toward`` (arms: raised; head: turned to the robot's LEFT),
-1 = the opposite limit. Gestures therefore follow whatever limits calibration found
and can never leave them. Every gesture is deliberately gentle: small amplitudes,
reduced speed (``speed`` is a fraction of each joint's max speed) and it ends at neutral.

Joints named in a keyframe but not configured are skipped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

from littlebot.motion.joints import JointSpec


@dataclass(frozen=True)
class Keyframe:
    pose: Mapping[str, float]  # joint -> normalised position (-1..1)
    duration: float = 0.0  # minimum travel time (s); the planner may take longer to respect limits
    hold: float = 0.0  # pause after arriving (s)
    speed: float = 1.0  # fraction (0..1] of each joint's max speed


@dataclass(frozen=True)
class Gesture:
    name: str
    description: str
    keyframes: tuple[Keyframe, ...]
    tags: tuple[str, ...] = field(default=())


def _kf(duration: float = 0.0, hold: float = 0.0, speed: float = 1.0, **pose: float) -> Keyframe:
    return Keyframe(pose=dict(pose), duration=duration, hold=hold, speed=speed)


_HOME = dict(head=0.0, left_arm=0.0, right_arm=0.0)


def _home(speed: float = 0.6, hold: float = 0.0) -> Keyframe:
    return Keyframe(pose=dict(_HOME), speed=speed, hold=hold)


GESTURES: dict[str, Gesture] = {g.name: g for g in (
    Gesture("neutral", "Return every joint calmly to its resting position.", (_home(speed=0.6),)),
    Gesture("wave", "Raise the right arm and wave gently three times.", (
        _kf(right_arm=0.70, head=-0.12, speed=0.8, hold=0.1),
        _kf(right_arm=0.85, speed=0.8),
        _kf(right_arm=0.65, speed=0.8),
        _kf(right_arm=0.85, speed=0.8),
        _kf(right_arm=0.65, speed=0.8),
        _kf(right_arm=0.85, speed=0.8),
        _kf(right_arm=0.70, speed=0.8, hold=0.2),
        _home(speed=0.7),
    ), ("social",)),
    Gesture("look_left", "Turn the head to the robot's left, pause, come back.", (
        _kf(head=0.70, speed=0.6, hold=1.0),
        _home(speed=0.6),
    ), ("attention",)),
    Gesture("look_right", "Turn the head to the robot's right, pause, come back.", (
        _kf(head=-0.70, speed=0.6, hold=1.0),
        _home(speed=0.6),
    ), ("attention",)),
    Gesture("raise_arms", "Lift both arms slowly ('so big!'), hold, lower.", (
        _kf(left_arm=0.80, right_arm=0.80, speed=0.6, hold=1.2),
        _home(speed=0.5),
    ), ("movement",)),
    Gesture("celebrate", "Arms up with two small pumps and a little head wiggle.", (
        _kf(left_arm=0.65, right_arm=0.65, speed=0.7),
        _kf(left_arm=0.80, right_arm=0.80, head=0.15, speed=0.7),
        _kf(left_arm=0.60, right_arm=0.60, head=-0.15, speed=0.7),
        _kf(left_arm=0.80, right_arm=0.80, head=0.15, speed=0.7),
        _kf(left_arm=0.65, right_arm=0.65, head=0.0, speed=0.7, hold=0.3),
        _home(speed=0.5),
    ), ("social", "celebration")),
    Gesture("dance", "Slow, small sway: head side to side with alternating arms.", (
        _kf(head=0.25, left_arm=0.30, right_arm=0.10, speed=0.4),
        _kf(head=-0.25, left_arm=0.10, right_arm=0.30, speed=0.4),
        _kf(head=0.25, left_arm=0.30, right_arm=0.10, speed=0.4),
        _kf(head=-0.25, left_arm=0.10, right_arm=0.30, speed=0.4),
        _home(speed=0.4),
    ), ("music", "movement")),
    Gesture("shake", "Gentle head shake (side to side) - the head only turns, it cannot nod.", (
        _kf(head=0.30, speed=0.6),
        _kf(head=-0.30, speed=0.6),
        _kf(head=0.30, speed=0.6),
        _kf(head=-0.30, speed=0.6),
        _home(speed=0.6),
    ), ("social",)),
)}

GESTURE_ORDER: tuple[str, ...] = ("wave", "look_left", "look_right", "raise_arms", "celebrate", "dance", "shake",
                                  "neutral")


def gesture_names() -> list[str]:
    return [n for n in GESTURE_ORDER if n in GESTURES] + sorted(set(GESTURES) - set(GESTURE_ORDER))


def get_gesture(name: str) -> Gesture:
    return GESTURES[name]


def resolve_pose(keyframe: Keyframe, specs: Mapping[str, JointSpec]) -> dict[str, float]:
    """Normalised keyframe -> absolute joint angles (only for configured joints)."""
    return {joint: specs[joint].resolve(value) for joint, value in keyframe.pose.items() if joint in specs}


def gesture_joints(gesture: Gesture, specs: Mapping[str, JointSpec]) -> frozenset[str]:
    """Joints a gesture actually moves (a closing neutral keyframe does not energise idle joints).

    A gesture that never leaves neutral (``neutral``) uses every joint it names.
    """
    named = frozenset(j for kf in gesture.keyframes for j in kf.pose if j in specs)
    moved = frozenset(j for kf in gesture.keyframes for j, v in kf.pose.items() if j in specs and abs(v) > 1e-9)
    return moved or named


def validate_gesture(gesture: Gesture, specs: Mapping[str, JointSpec]) -> list[str]:
    errs: list[str] = []
    if not gesture.keyframes:
        errs.append(f"{gesture.name}: no keyframes")
    for i, kf in enumerate(gesture.keyframes):
        where = f"{gesture.name}[{i}]"
        if not 0 < kf.speed <= 1.0:
            errs.append(f"{where}: speed must be in (0, 1]")
        if kf.duration < 0 or kf.hold < 0 or not math.isfinite(kf.duration) or not math.isfinite(kf.hold):
            errs.append(f"{where}: duration/hold must be >= 0")
        for joint, value in kf.pose.items():
            if not math.isfinite(value) or not -1.0 <= value <= 1.0:
                errs.append(f"{where}: {joint} position {value} outside -1..1")
        for joint, angle in resolve_pose(kf, specs).items():
            if not specs[joint].is_within(angle):
                errs.append(f"{where}: {joint} angle {angle:.1f} outside limits")
    last = gesture.keyframes[-1] if gesture.keyframes else None
    if last is not None and any(abs(v) > 1e-9 for v in last.pose.values()):
        errs.append(f"{gesture.name}: must end at neutral")
    return errs


def validate_gestures(specs: Mapping[str, JointSpec]) -> list[str]:
    errs: list[str] = []
    for g in GESTURES.values():
        errs.extend(validate_gesture(g, specs))
    return errs
