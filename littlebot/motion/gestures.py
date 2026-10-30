"""Gesture library: short keyframe sequences in normalised joint positions.

Positions are normalised per joint (see ``JointSpec.resolve``): 0 = neutral, +1 = the
limit named by ``positive_toward`` (arms: raised; ``head`` (pan): turned to the robot's
LEFT; ``head_tilt``: looking UP), -1 = the opposite limit. Gestures therefore follow
whatever limits calibration found and can never leave them. Every gesture is deliberately
gentle: small amplitudes (|position| <= 0.85), reduced speed (``speed`` is a fraction of
each joint's max speed, <= 0.8) and it ends at neutral.

Joints named in a keyframe but not configured are skipped, so every gesture works with any
subset of joints (no ``head_tilt`` servo: ``nod`` has nothing to move and is refused, the
others simply leave that axis out).
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


_HOME = dict(head=0.0, head_tilt=0.0, left_arm=0.0, right_arm=0.0)


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
    Gesture("celebrate", "Arms up with two small pumps, head up with a little wiggle.", (
        _kf(left_arm=0.65, right_arm=0.65, head_tilt=0.20, speed=0.7),
        _kf(left_arm=0.80, right_arm=0.80, head=0.15, head_tilt=0.30, speed=0.7),
        _kf(left_arm=0.60, right_arm=0.60, head=-0.15, head_tilt=0.20, speed=0.7),
        _kf(left_arm=0.80, right_arm=0.80, head=0.15, head_tilt=0.30, speed=0.7),
        _kf(left_arm=0.65, right_arm=0.65, head=0.0, head_tilt=0.15, speed=0.7, hold=0.3),
        _home(speed=0.5),
    ), ("social", "celebration")),
    Gesture("dance", "Slow, small sway: head side to side and bobbing, with alternating arms.", (
        _kf(head=0.25, head_tilt=0.15, left_arm=0.30, right_arm=0.10, speed=0.4),
        _kf(head=-0.25, head_tilt=-0.10, left_arm=0.10, right_arm=0.30, speed=0.4),
        _kf(head=0.25, head_tilt=0.15, left_arm=0.30, right_arm=0.10, speed=0.4),
        _kf(head=-0.25, head_tilt=-0.10, left_arm=0.10, right_arm=0.30, speed=0.4),
        _home(speed=0.4),
    ), ("music", "movement")),
    Gesture("shake", "Gentle head shake (side to side) on the pan axis only.", (
        _kf(head=0.30, speed=0.6),
        _kf(head=-0.30, speed=0.6),
        _kf(head=0.30, speed=0.6),
        _kf(head=-0.30, speed=0.6),
        _home(speed=0.6),
    ), ("social",)),
    # ---- head pan + tilt and conversation gestures (body language: littlebot/motion/body.py) ----
    Gesture("nod", "Yes-nod: the head dips down and back up twice (needs the head_tilt servo).", (
        _kf(head_tilt=-0.45, speed=0.7),
        _kf(head_tilt=0.10, speed=0.7),
        _kf(head_tilt=-0.35, speed=0.7),
        _home(speed=0.6),
    ), ("social", "conversation")),
    Gesture("nod_small", "One small nod, as at the end of a sentence (needs the head_tilt servo).", (
        _kf(head_tilt=-0.30, speed=0.6),
        _home(speed=0.5),
    ), ("conversation",)),
    Gesture("head_shake", "Small 'no' / 'hmm, not sure': quick little shake with the chin slightly down.", (
        _kf(head=0.22, head_tilt=-0.10, speed=0.7),
        _kf(head=-0.22, head_tilt=-0.10, speed=0.7),
        _kf(head=0.16, head_tilt=-0.10, speed=0.7),
        _kf(head=-0.12, head_tilt=-0.05, speed=0.7),
        _home(speed=0.6),
    ), ("social", "conversation")),
    Gesture("tilt_curious", "Curious: the head turns a little to one side and lifts, as if asking 'hmm?'.", (
        _kf(head=0.22, head_tilt=0.35, left_arm=0.12, speed=0.5, hold=1.0),
        _home(speed=0.5),
    ), ("conversation",)),
    Gesture("look_up_think", "Thinking: look up and to the side, one arm lifts a little (hand to chin).", (
        _kf(head=-0.30, head_tilt=0.55, right_arm=0.30, speed=0.5, hold=1.4),
        _home(speed=0.4),
    ), ("conversation",)),
    Gesture("shrug", "Shrug: both arms lift a little while the head dips and turns slightly, then relax.", (
        _kf(left_arm=0.35, right_arm=0.35, head=0.10, head_tilt=-0.20, speed=0.7, hold=0.5),
        _home(speed=0.5),
    ), ("conversation",)),
    Gesture("perk_up", "Listening: perk up - the head lifts a little, the arms twitch up, then settle.", (
        _kf(head_tilt=0.30, left_arm=0.12, right_arm=0.12, speed=0.7, hold=0.8),
        _home(speed=0.4),
    ), ("conversation",)),
    Gesture("point_screen", "Look at this: the right arm points at the screen while the head dips toward it.", (
        _kf(right_arm=0.50, head=-0.08, head_tilt=-0.12, speed=0.6, hold=0.4),
        _kf(right_arm=0.58, speed=0.5),
        _kf(right_arm=0.50, speed=0.5, hold=1.0),
        _home(speed=0.5),
    ), ("conversation", "screen")),
    Gesture("wave_hello", "Hello: the right arm goes up for three cheerful waves, the head lifts.", (
        _kf(right_arm=0.70, head=-0.12, head_tilt=0.20, speed=0.8, hold=0.1),
        _kf(right_arm=0.85, speed=0.8),
        _kf(right_arm=0.65, speed=0.8),
        _kf(right_arm=0.85, speed=0.8),
        _kf(right_arm=0.65, speed=0.8),
        _kf(right_arm=0.85, speed=0.8),
        _kf(right_arm=0.70, speed=0.8, hold=0.2),
        _home(speed=0.7),
    ), ("social",)),
    Gesture("wave_goodbye", "Goodbye: two slower waves, then the head dips a little.", (
        _kf(right_arm=0.65, head=-0.10, speed=0.6),
        _kf(right_arm=0.80, speed=0.6),
        _kf(right_arm=0.60, speed=0.6),
        _kf(right_arm=0.80, speed=0.6),
        _kf(right_arm=0.60, head_tilt=-0.15, speed=0.5, hold=0.4),
        _home(speed=0.5),
    ), ("social",)),
    Gesture("arm_wiggle", "Happy wiggle: the arms twitch up a tiny bit, left-right-left.", (
        _kf(left_arm=0.18, right_arm=0.08, speed=0.7),
        _kf(left_arm=0.08, right_arm=0.18, speed=0.7),
        _kf(left_arm=0.18, right_arm=0.08, speed=0.7),
        _home(speed=0.6),
    ), ("conversation",)),
    Gesture("droop", "Sad: the head slowly lowers a little, rests, then comes back up.", (
        _kf(head_tilt=-0.45, speed=0.35, hold=1.5),
        _home(speed=0.35),
    ), ("conversation",)),
    Gesture("idle_shift", "Idle: a tiny, slow shift of the head, as if glancing around.", (
        _kf(head=0.10, head_tilt=0.08, speed=0.25, hold=1.2),
        _home(speed=0.25),
    ), ("idle",)),
)}

GESTURE_ORDER: tuple[str, ...] = (
    "wave", "look_left", "look_right", "raise_arms", "celebrate", "dance", "shake",
    "nod", "nod_small", "head_shake", "tilt_curious", "look_up_think", "shrug", "perk_up", "point_screen",
    "wave_hello", "wave_goodbye", "arm_wiggle", "droop", "idle_shift",
    "neutral",
)


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
