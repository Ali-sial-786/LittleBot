"""Expression parameter sets for LittleBot's eyes.

Every expression is a plain data description of two eyes plus a few global
modifiers (colour tint, glow, gaze bias, idle bounce, overlay extras). The
animator interpolates between them; the renderer never needs to know an
expression's name.

Design units are pixels at the native 480x320 screen. Conventions:

* ``tilt`` and ``lid_angle`` are *mirrored* per eye so symmetric expressions
  stay symmetric: ``tilt > 0`` leans the top of the eye outwards; ``lid_angle
  > 0`` makes the upper lid droop towards the outer corner (sad / sleepy),
  ``< 0`` towards the inner corner (determined / confused).
* ``dx`` / ``dy`` are plain screen offsets (not mirrored).
* "left" / "right" are the viewer's left / right (screen space).

No pygame here: this module is pure data and is safe to import anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from littlebot.core.interfaces import EXPRESSIONS, FACE_STATES

# Overlay extras an expression may request (faded in/out by the animator).
EXTRAS: tuple[str, ...] = (
    "dots",  # thinking "..." typing indicator
    "ring",  # listening: soft pulsing frame so a parent can see LittleBot is listening
    "question",  # confused "?"
    "hearts",  # affectionate floating hearts
    "sparkles",  # twinkles (excited / celebrating)
    "confetti",  # celebrating confetti rain
    "zzz",  # sleepy Z's
    "blush",  # rosy cheeks
    "mouth",  # small cute mouth (also shown automatically while speaking)
)

# Expression adopted by default for each face state (and after a ``hold`` expires).
DEFAULT_EXPRESSION_FOR_STATE: dict[str, str] = {
    "idle": "neutral",
    "listening": "listening",
    "thinking": "thinking",
    "speaking": "speaking",
    "sleeping": "sleepy",
    "error": "confused",
    "hidden": "neutral",
}

# Numeric fields that have a hard valid range (used for clamping and tests).
EYE_RANGES: dict[str, tuple[float, float]] = {
    "width": (0.3, 1.8),
    "height": (0.3, 1.8),
    "scale": (0.3, 1.8),
    "roundness": (0.05, 0.5),
    "openness": (0.0, 1.0),
    "lid_top": (0.0, 0.9),
    "lid_angle": (-35.0, 35.0),
    "lid_bottom": (0.0, 0.9),
    "smile": (0.0, 1.0),
    "tilt": (-30.0, 30.0),
    "pupil": (0.0, 1.5),
    "dx": (-60.0, 60.0),
    "dy": (-60.0, 60.0),
}


@dataclass(frozen=True)
class EyeParams:
    """Shape of one eye (all values interpolate linearly)."""

    width: float = 1.0  # multiplier of the base eye width
    height: float = 1.0  # multiplier of the base eye height
    scale: float = 1.0  # uniform multiplier (applied on top of width/height)
    roundness: float = 0.50  # corner radius as a fraction of the smaller side (0.5 = pill / circle)
    openness: float = 1.0  # vertical squash: 1 open .. 0 closed line
    lid_top: float = 0.0  # fraction covered by the upper lid
    lid_angle: float = 0.0  # degrees, + droops towards the outer corner (mirrored per eye)
    lid_bottom: float = 0.0  # fraction covered by a flat lower lid (squint)
    smile: float = 0.0  # happy "^" crescent: lower edge curves up
    tilt: float = 0.0  # degrees, + leans the top of the eye outwards (mirrored per eye)
    pupil: float = 1.0  # pupil size multiplier (big = soft "puppy" eyes, small = wide-eyed)
    dx: float = 0.0  # screen offset px
    dy: float = 0.0


@dataclass(frozen=True)
class ExpressionParams:
    """Both eyes plus global look modifiers."""

    left: EyeParams = field(default_factory=EyeParams)
    right: EyeParams = field(default_factory=EyeParams)
    tint: tuple[float, float, float] = (90.0, 210.0, 255.0)  # colour mixed into the eye colour
    tint_mix: float = 0.0  # 0 = configured eye colour, 1 = tint colour
    brightness: float = 1.0
    glow: float = 1.0  # glow intensity multiplier
    shine: float = 1.0  # specular highlight strength
    gaze_x: float = 0.0  # gaze bias, -1 (left) .. 1 (right)
    gaze_y: float = 0.0  # gaze bias, -1 (up) .. 1 (down)
    spacing: float = 0.0  # extra px between the eyes
    bob: float = 0.0  # bouncy idle hop amplitude (px)
    bob_hz: float = 0.0  # hops per second
    sway: float = 0.0  # gentle side-to-side rocking (degrees)
    wander: float = 1.0  # idle gaze wandering amount (0 = hold still)
    blink_rate: float = 1.0  # multiplier on the blink interval (>1 = blinks less often)
    blink_speed: float = 1.0  # multiplier on blink duration (>1 = slower, heavier blinks)
    extras: frozenset[str] = frozenset()


_EYE_FIELDS = tuple(f.name for f in fields(EyeParams))
_EXPR_FLOAT_FIELDS = tuple(
    f.name for f in fields(ExpressionParams) if f.name not in ("left", "right", "tint", "extras")
)


def _sym(**kw: Any) -> tuple[EyeParams, EyeParams]:
    """Both eyes identical (tilt / lid_angle are mirrored by the renderer)."""
    e = EyeParams(**kw)
    return e, e


def _expr(eyes: tuple[EyeParams, EyeParams], **kw: Any) -> ExpressionParams:
    extras = kw.pop("extras", ())
    return ExpressionParams(left=eyes[0], right=eyes[1], extras=frozenset(extras), **kw)


SAD_BLUE = (70.0, 135.0, 255.0)
LOVE_PINK = (255.0, 140.0, 205.0)
WARM_GOLD = (255.0, 214.0, 120.0)
SOFT_LAVENDER = (175.0, 165.0, 255.0)

EXPRESSION_PARAMS: dict[str, ExpressionParams] = {
    "neutral": _expr(_sym()),
    "happy": _expr(
        _sym(width=1.1, height=0.92, smile=0.68, roundness=0.5, dy=-2),
        glow=1.15, shine=0.6, wander=0.8,
    ),
    "excited": _expr(
        _sym(width=1.08, height=1.10, scale=1.08, smile=0.34, pupil=1.08, dy=-4),
        glow=1.3, shine=1.3, bob=5.0, bob_hz=1.9, blink_rate=1.3, extras=("sparkles",),
    ),
    "curious": _expr(
        (
            EyeParams(scale=1.14, pupil=1.08, tilt=-4, dy=-6),
            EyeParams(scale=0.88, lid_top=0.16, lid_angle=-5, pupil=0.95, tilt=4, dy=2),
        ),
        gaze_x=0.22, gaze_y=-0.12, wander=0.5, sway=2.5, shine=1.2,
    ),
    "surprised": _expr(
        _sym(width=1.05, height=1.16, scale=1.12, pupil=0.78, dy=-6),
        glow=1.35, shine=1.4, spacing=10, wander=0.15, blink_rate=1.8,
    ),
    "sleepy": _expr(
        _sym(height=0.92, lid_top=0.5, lid_angle=4, pupil=0.86, dy=6),
        brightness=0.78, glow=0.65, shine=0.3, gaze_y=0.35, wander=0.25,
        blink_rate=0.7, blink_speed=2.2, extras=("zzz",),
    ),
    "sad": _expr(
        _sym(height=0.9, width=0.96, lid_top=0.28, lid_angle=16, pupil=1.15, dy=8),
        tint=SAD_BLUE, tint_mix=0.55, brightness=0.9, glow=0.8, shine=1.1,
        gaze_y=0.4, wander=0.3, blink_speed=1.4,
    ),
    "thinking": _expr(
        (
            EyeParams(lid_top=0.2, lid_angle=-6, pupil=0.95),
            EyeParams(scale=1.04, pupil=0.95),
        ),
        gaze_x=0.5, gaze_y=-0.6, wander=0.0, sway=0.0, extras=("dots",),
    ),
    "listening": _expr(
        (
            EyeParams(scale=1.06, height=1.05, pupil=1.1, dy=-3, tilt=3),
            EyeParams(scale=1.06, height=1.05, pupil=1.1, dy=3, tilt=-3),
        ),
        glow=1.15, shine=1.2, wander=0.25, blink_rate=1.2, extras=("ring",),
    ),
    "speaking": _expr(
        _sym(height=0.98, dy=-8),
        glow=1.05, wander=0.5, extras=("mouth",),
    ),
    "confused": _expr(
        (
            EyeParams(scale=0.88, lid_top=0.24, lid_angle=-8, pupil=0.9, dy=4),
            EyeParams(scale=1.1, dy=-8, tilt=5),
        ),
        gaze_x=-0.2, gaze_y=-0.15, wander=0.4, sway=3.0, extras=("question",),
    ),
    "playful": _expr(
        (
            EyeParams(width=1.1, height=0.92, smile=0.68, roundness=0.5, dy=-2, tilt=-4),
            EyeParams(scale=1.08, pupil=1.08, dy=-6, tilt=4),
        ),
        gaze_x=0.15, glow=1.15, shine=1.2, bob=3.0, bob_hz=1.4, sway=4.0, wander=0.6,
    ),
    "affectionate": _expr(
        _sym(width=1.06, height=0.9, smile=0.6, openness=0.94, roundness=0.5, dy=-2),
        tint=LOVE_PINK, tint_mix=0.72, glow=1.25, shine=0.7, wander=0.4, blink_speed=1.3,
        extras=("hearts", "blush"),
    ),
    "celebrating": _expr(
        _sym(width=1.1, height=0.98, scale=1.1, smile=0.66, roundness=0.5, dy=-6),
        tint=WARM_GOLD, tint_mix=0.12, glow=1.4, shine=1.0, bob=6.0, bob_hz=2.2, sway=3.0,
        blink_rate=1.5, extras=("sparkles", "confetti", "blush"),
    ),
}

# Face states can add a colour mood on top of the expression (kept gentle).
STATE_TINTS: dict[str, tuple[tuple[float, float, float], float]] = {
    "error": (SOFT_LAVENDER, 0.45),
}

assert set(EXPRESSION_PARAMS) == set(EXPRESSIONS), "every EXPRESSION needs a parameter set"
assert set(DEFAULT_EXPRESSION_FOR_STATE) == set(FACE_STATES)


def get_expression(name: str) -> ExpressionParams:
    """Parameter set for ``name`` (KeyError if unknown)."""
    return EXPRESSION_PARAMS[name]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_eye(a: EyeParams, b: EyeParams, t: float) -> EyeParams:
    return EyeParams(**{n: _lerp(getattr(a, n), getattr(b, n), t) for n in _EYE_FIELDS})


def lerp_expression(a: ExpressionParams, b: ExpressionParams, t: float) -> ExpressionParams:
    """Linear interpolation. ``extras`` switch to the target (the animator fades them).

    A side with ``tint_mix == 0`` borrows the other side's tint so colours never
    pass through an unrelated hue while fading in or out.
    """
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    ta = b.tint if a.tint_mix <= 0.0 else a.tint
    tb = a.tint if b.tint_mix <= 0.0 else b.tint
    vals = {n: _lerp(getattr(a, n), getattr(b, n), t) for n in _EXPR_FLOAT_FIELDS}
    return ExpressionParams(
        left=lerp_eye(a.left, b.left, t),
        right=lerp_eye(a.right, b.right, t),
        tint=(_lerp(ta[0], tb[0], t), _lerp(ta[1], tb[1], t), _lerp(ta[2], tb[2], t)),
        extras=b.extras,
        **vals,
    )


def clamp_eye(e: EyeParams) -> EyeParams:
    """Clamp every field into its valid range."""
    changes = {}
    for n, (lo, hi) in EYE_RANGES.items():
        v = getattr(e, n)
        if v < lo or v > hi:
            changes[n] = min(hi, max(lo, v))
    return replace(e, **changes) if changes else e
